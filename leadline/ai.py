"""AI processing layer (spec §6): Ollama primary, Anthropic Haiku fallback.

Both backends get the same prompt and must return the ArticleSummary JSON
shape. Provider used is logged per article. A hard daily cap guards the
Anthropic path (spec §10).

A local model has to be loaded into memory before it can answer, and a cold
load plus a summary outlasts any sensible request timeout. monitor_ollama()
loads the model at launch and while the reader is active, and tracks whether it
is in memory: summaries wait for the load instead of timing out, or — with
Claude's "primary_during_load" role — go to Claude until Ollama is ready.
"""
import json
import re
import threading
import time

import requests

from . import config, store

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "straight_headline": {"type": "string"},
        "bluf_bullets": {"type": "array", "items": {"type": "string"},
                         "minItems": 3, "maxItems": 5},
        "one_sentence": {"type": "string"},
        "topic_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["straight_headline", "bluf_bullets", "one_sentence",
                 "topic_tags", "confidence"],
}

PROMPT_TEMPLATE = """You are a news editor who rewrites clickbait headlines and produces factual summaries.

ARTICLE TITLE: {original_headline}
SOURCE: {source_name}
ARTICLE BODY:
---
{body}
---

Return a JSON object with:
- straight_headline: factual, declarative, <=16 words. State what happened AND the most
  important concrete detail: what the subject actually does, changes, decides, costs, or
  affects. Never leave the subject generic ("a new bill", "a major policy", "a tech company")
  when the body says specifically what it is or does. Example: not "New housing bill becomes
  law" but "Housing bill limiting investor purchases and easing modular-home rules becomes law".
  No teasers.
- bluf_bullets: 3-5 bullets, each a complete sentence with the key facts. Do not repeat the
  headline's detail verbatim; add specifics (numbers, names, dates, consequences).
- one_sentence: the single most important fact in <=25 words.
- topic_tags: up to 4 topic labels.
- confidence: float 0-1 reflecting how well the body supports the summary.

Return ONLY valid JSON. No preamble."""


def build_prompt(article, source_name):
    return PROMPT_TEMPLATE.format(
        original_headline=article["original_headline"],
        source_name=source_name or "Unknown",
        body=(article.get("body_text") or "")[:4000],
    )


def _parse_summary(text):
    """Validate model output against the ArticleSummary shape (spec §6.3)."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in response")
    data = json.loads(match.group(0))
    bullets = [str(b).strip() for b in data["bluf_bullets"] if str(b).strip()]
    if not 3 <= len(bullets) <= 5:
        raise ValueError("bluf_bullets must have 3-5 items")
    return {
        "straight_headline": str(data["straight_headline"]).strip(),
        "bluf_bullets": bullets,
        "one_sentence": str(data["one_sentence"]).strip(),
        "topic_tags": [str(t).strip() for t in data.get("topic_tags", [])][:4],
        "confidence": min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
    }


# --- Ollama model readiness ---

_WAITING = ("checking", "idle", "loading")   # states that can still become ready
DEMAND_WINDOW_SECONDS = 600    # keep the model loaded this long after reading activity
ERROR_SHOWN_SECONDS = 900      # how long a failed summary shows in the status pill

_health = threading.Condition()
_ollama = {"state": "checking", "target": None, "since": time.time(), "message": None}
_last_error = {}               # provider -> (timestamp, short reason); cleared on success
_last_demand = time.time()     # launching the app counts as reading activity
_wake = threading.Event()
_ollama_serial = threading.Lock()   # one generation at a time, so queued requests
                                    # don't spend their timeout waiting in line


def _ollama_target():
    return config.setting("ollama_base_url").rstrip("/"), config.setting("ollama_model")


def _set_ollama(state, target, message=None):
    with _health:
        if (state, target) != (_ollama["state"], _ollama["target"]):
            _ollama.update(state=state, target=target, since=time.time())
            if state == "ready":
                _last_error.pop("ollama", None)
        _ollama["message"] = message
        _health.notify_all()


def _same_model(a, b):
    """Ollama reports an untagged 'llama3.2' as 'llama3.2:latest'."""
    def tagged(m):
        return m if ":" in m.rsplit("/", 1)[-1] else m + ":latest"
    return tagged(a) == tagged(b)


def _probe_ollama(base_url, model):
    """'ready' if the model is in memory, 'idle' if the server is up without it."""
    try:
        resp = requests.get(f"{base_url}/api/ps",
                            timeout=(config.OLLAMA_CONNECT_TIMEOUT_SECONDS, 10))
    except requests.RequestException:
        return "unreachable"
    try:
        loaded = [m.get("name") or m.get("model") or "" for m in resp.json().get("models", [])]
    except (ValueError, AttributeError):
        loaded = []   # no /api/ps on older servers; loading a loaded model is instant
    return "ready" if any(_same_model(n, model) for n in loaded) else "idle"


def _load_ollama(base_url, model):
    """An empty generate request loads the model and returns once it is in memory."""
    try:
        resp = requests.post(f"{base_url}/api/generate", json={"model": model},
                             timeout=(config.OLLAMA_CONNECT_TIMEOUT_SECONDS,
                                      config.OLLAMA_LOAD_TIMEOUT_SECONDS))
    except requests.ConnectionError:
        return "unreachable", None
    except requests.Timeout:
        return "error", f"load took over {config.OLLAMA_LOAD_TIMEOUT_SECONDS}s"
    except requests.RequestException as e:
        return "error", _short_error(e)
    if resp.status_code == 404:
        return "missing", None
    if not resp.ok:
        return "error", f"HTTP {resp.status_code}"
    return "ready", None


def _check_ollama():
    target = _ollama_target()
    if config.setting("ollama_role") == "off":
        return _set_ollama("off", target)
    state, message = _probe_ollama(*target), None
    if state == "idle" and time.time() - _last_demand < DEMAND_WINDOW_SECONDS:
        _set_ollama("loading", target)
        state, message = _load_ollama(*target)
    _set_ollama(state, target, message)


def monitor_ollama():
    """Background thread: keep the Ollama model's state current, and load it
    while the reader is active (launch, reading, settings changes). Once the
    reader goes idle the server unloads it on its own keep-alive schedule."""
    while True:
        _wake.clear()
        try:
            _check_ollama()
        except Exception as e:
            print(f"[ollama] monitor error: {e}")
        _wake.wait(15 if _ollama["state"] in ("idle", "unreachable") else 60)


def ollama_ready():
    target = _ollama_target()
    with _health:
        return _ollama["state"] == "ready" and _ollama["target"] == target


def note_demand():
    """The reader wants summaries: get (or keep) the Ollama model loaded."""
    global _last_demand
    _last_demand = time.time()
    if not ollama_ready():
        _wake.set()


def settings_changed():
    """New server, model, role, or key: forget stale errors and re-check now."""
    _last_error.clear()
    note_demand()
    _wake.set()


def _await_ollama(target):
    """Hold a summary while the model loads, so the load can't use up the
    generation timeout; raise at once if Ollama can't serve it."""
    note_demand()
    with _health:
        _health.wait_for(
            lambda: _ollama["target"] == target and _ollama["state"] not in _WAITING,
            timeout=config.OLLAMA_LOAD_TIMEOUT_SECONDS + 30)
        state = _ollama["state"] if _ollama["target"] == target else "checking"
        message = _ollama["message"]
    if state != "ready":
        raise RuntimeError(f"Ollama {state}" + (f": {message}" if message else ""))


def _short_error(e):
    """One-line reason for the status pill."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} {e.response.reason or ''}".strip()
    if isinstance(e, requests.ConnectionError):
        return "server unreachable"
    if isinstance(e, requests.Timeout):
        return "timed out"
    return (str(e) or type(e).__name__)[:120]


def _recent_error(provider, now):
    err = _last_error.get(provider)
    return err[1] if err and now - err[0] < ERROR_SHOWN_SECONDS else None


# --- backends ---

_cant_skip_thinking = set()   # models that return nothing with "think": false (gpt-oss)


def _ollama_chat(base_url, model, prompt, skip_thinking):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": SUMMARY_SCHEMA,
    }
    if skip_thinking:
        body["think"] = False
    resp = requests.post(f"{base_url}/api/chat", json=body,
                         timeout=(config.OLLAMA_CONNECT_TIMEOUT_SECONDS,
                                  config.OLLAMA_TIMEOUT_SECONDS))
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def call_ollama(prompt):
    """A summary doesn't need a reasoning pass, and thinking models (gemma4,
    qwen3) spend several times longer on one — so ask them to skip it. Models
    that can't (gpt-oss) answer empty; remember those and ask normally."""
    base_url, model = target = _ollama_target()
    _await_ollama(target)
    try:
        with _ollama_serial:
            skip = model not in _cant_skip_thinking
            content = _ollama_chat(base_url, model, prompt, skip)
            if skip and not content.strip():
                _cant_skip_thinking.add(model)
                content = _ollama_chat(base_url, model, prompt, False)
    except requests.RequestException:
        _wake.set()   # re-probe: the server may be down or have dropped the model
        raise
    return _parse_summary(content)


def call_anthropic(prompt):
    api_key = config.setting("anthropic_api_key")
    if not api_key:
        raise RuntimeError("Anthropic API key not set")
    if store.anthropic_calls_today() >= config.MAX_ANTHROPIC_DAILY_ARTICLES:
        raise RuntimeError("daily Anthropic article cap reached")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": config.setting("anthropic_model"),
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return _parse_summary(resp.json()["content"][0]["text"])


_BACKENDS = {
    "ollama": (call_ollama, "ollama_model"),
    "anthropic": (call_anthropic, "anthropic_model"),
}


def enabled_backends():
    """Providers in role order: primaries, then secondaries; 'off' excluded.
    Claude's 'primary_during_load' role ranks it ahead of Ollama until Ollama's
    model is in memory, and behind it (as the fallback) from then on."""
    rank = {"primary": 0, "secondary": 1}
    ranked = []
    for name in ("ollama", "anthropic"):
        role = config.setting(f"{name}_role")
        if role == "primary_during_load":
            ranked.append((1 if ollama_ready() else -1, name))
        elif role in rank:
            ranked.append((rank[role], name))
    return [name for _, name in sorted(ranked)]


def backend_status():
    """Snapshot for the UI's status pill (no network): each server's state
    plus a one-line explanation."""
    now = time.time()
    base_url, model = target = _ollama_target()
    with _health:
        o = dict(_ollama)
    state = o["state"] if o["target"] == target else "checking"
    if config.setting("ollama_role") == "off":
        state = "off"
    detail = {
        "off": "Off",
        "checking": f"Checking {base_url}…",
        "unreachable": f"Server unreachable at {base_url}",
        "idle": f"{model} not in memory; loads when you start reading",
        "loading": f"Loading {model} into memory… {int(now - o['since'])}s",
        "ready": f"{model} loaded and ready",
        "missing": f"{model} isn't installed on {base_url}",
        "error": f"Couldn't load {model}: {o['message']}",
    }[state]
    if state == "ready" and (err := _recent_error("ollama", now)):
        state, detail = "degraded", f"{model} loaded, but the last summary failed: {err}"

    role = config.setting("anthropic_role")
    if role == "off":
        claude, note = "off", "Off"
    elif not config.setting("anthropic_api_key"):
        claude, note = "error", "No API key set"
    elif err := _recent_error("anthropic", now):
        claude, note = "error", f"Last summary failed: {err}"
    elif state == "off":
        claude, note = "active", "Summarizing (Ollama is off)"
    elif enabled_backends()[0] == "anthropic":
        claude, note = "active", ("Primary" if role == "primary"
                                  else "Covering while Ollama loads" if state == "loading"
                                  else "Covering until Ollama is ready")
    elif state in ("unreachable", "missing", "error"):
        claude, note = "active", "Covering while Ollama is unavailable"
    else:
        claude, note = "standby", ("Standing by; Ollama is ready" if role == "primary_during_load"
                                   else "Secondary; used if Ollama fails")
    if claude in ("active", "standby"):
        note = f"{config.setting('anthropic_model')} · {note}"
    return {"ollama": {"state": state, "detail": detail},
            "anthropic": {"state": claude, "detail": note}}


def summarize(article, source_name):
    """Router: try servers in the user's primary/secondary order (spec §6.1).
    Returns (summary, provider, model) or raises if all enabled backends fail."""
    backends = enabled_backends()
    if not backends:
        raise RuntimeError("all AI servers are set to off")
    prompt = build_prompt(article, source_name)
    errors = []
    for provider in backends:
        fn, model_key = _BACKENDS[provider]
        try:
            summary = fn(prompt)
        except Exception as e:  # timeout, connection, malformed output
            _last_error[provider] = (time.time(), _short_error(e))
            errors.append(f"[{provider}] {e}")
            continue
        _last_error.pop(provider, None)
        return summary, provider, config.setting(model_key)
    raise RuntimeError("; ".join(errors))


def summarize_articles(article_ids):
    """Summarize specific articles (read-ahead window), on demand only.
    Re-extracts the body first if the TTL purge already dropped it.
    Returns the ids that could not be summarized."""
    from . import ingest

    feeds = {f["id"]: f["name"] for f in store.get_feeds()}
    failed = []
    for article_id in article_ids:
        article = store.get_article(article_id)
        if not article or article["processed"]:
            continue
        if not article["body_text"]:
            ingest.extract_article(article)
            article = store.get_article(article_id)
        try:
            summary, provider, model = summarize(article, feeds.get(article["feed_source_id"]))
        except RuntimeError:
            failed.append(article_id)   # card keeps its original headline; retried later
            continue
        store.save_summary(article_id, summary, provider, model)
    return failed
