"""AI processing layer (spec §6): Ollama primary, Anthropic Haiku fallback.

Both backends get the same prompt and must return the ArticleSummary JSON
shape. Provider used is logged per article. A hard daily cap guards the
Anthropic path (spec §10).

A local model has to be loaded into memory before it can answer, and a cold
load plus a summary outlasts any sensible request timeout. monitor_ollama()
loads the model at launch and while the reader is active, and tracks whether it
is in memory: summaries wait for the load instead of timing out, or — with
Claude's "primary_during_load" role — go to Claude until Ollama is ready.
If another model is already in memory at launch, the reader is asked whether
to use it for the session rather than wait for the configured one to load.
"""
import json
import re
import threading
import time

import requests

from . import config, ingest, store

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

_WAITING = ("checking", "idle", "loading", "choice")   # states that can still become ready
DEMAND_WINDOW_SECONDS = 600    # keep the model loaded this long after reading activity
ERROR_SHOWN_SECONDS = 900      # how long a failed summary shows in the status pill

_health = threading.Condition()
_ollama = {"state": "checking", "target": None, "since": time.time(), "message": None}
_last_error = {}               # provider -> (timestamp, short reason); cleared on success
_last_demand = time.time()     # launching the app counts as reading activity
_wake = threading.Event()
_ollama_serial = threading.Lock()   # one generation at a time, so queued requests
                                    # don't spend their timeout waiting in line
_abort_epoch = 0       # bumped by abandon(); calls that began under an older one give up
_offer = None          # {"configured", "loaded": [...]}: launch question awaiting an answer
_offer_made = False    # asked (or found the configured model loaded) once this session
_session_model = None  # (base_url, configured, chosen): loaded model picked at launch


def _ollama_target():
    """(server, model) to use: the configured model, unless the reader chose an
    already-loaded one at launch and hasn't since changed server or model."""
    base_url = config.setting("ollama_base_url").rstrip("/")
    model = config.setting("ollama_model")
    if _session_model and _session_model[:2] == (base_url, model):
        return base_url, _session_model[2]
    return base_url, model


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


def _is_embedding(m):
    """Embedding models can be loaded but can't write a summary."""
    family = ((m.get("details") or {}).get("family") or "").lower()
    return "embed" in (m.get("name") or "").lower() or family.endswith("bert")


def _probe_ollama(base_url, model):
    """('ready' if the model is in memory, 'idle' if the server is up without
    it, or 'unreachable'; the other chat models currently in memory)."""
    try:
        resp = requests.get(f"{base_url}/api/ps",
                            timeout=(config.OLLAMA_CONNECT_TIMEOUT_SECONDS, 10))
    except requests.RequestException:
        return "unreachable", []
    try:
        models = [m for m in resp.json().get("models", []) if not _is_embedding(m)]
        loaded = [m.get("name") or m.get("model") or "" for m in models]
    except (ValueError, AttributeError):
        loaded = []   # no /api/ps on older servers; loading a loaded model is instant
    if any(_same_model(n, model) for n in loaded):
        return "ready", []
    return "idle", [n for n in loaded if n]


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
    global _offer, _offer_made
    target = _ollama_target()
    if config.setting("ollama_role") == "off":
        return _set_ollama("off", target)
    if _offer:
        return _set_ollama("choice", target)   # don't load until the reader answers
    state, others = _probe_ollama(*target)
    message = None
    if state != "unreachable" and not _offer_made:
        _offer_made = True
        if state == "idle" and others:
            _offer = {"configured": target[1], "loaded": others}
            return _set_ollama("choice", target)
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


def model_offer():
    """The launch question for the UI, or None once answered."""
    return dict(_offer) if _offer else None


def choose_model(model):
    """The reader's answer: an already-loaded model to use this session, or
    the configured one (which then loads as usual)."""
    global _offer, _session_model
    if not _offer:
        return
    base_url = config.setting("ollama_base_url").rstrip("/")
    configured = config.setting("ollama_model")
    if model in _offer["loaded"] and not _same_model(model, configured):
        _session_model = (base_url, configured, model)
    _offer = None
    _set_ollama("checking", _ollama_target())
    _wake.set()


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
    """New server, model, role, or key: forget stale errors and re-check now.
    Saving settings also answers a pending launch question with the saved model."""
    global _offer
    _offer = None
    _last_error.clear()
    note_demand()
    _wake.set()


class Cancelled(Exception):
    """The reader resubmitted while this call was waiting or generating. Not a
    RuntimeError: callers that treat those as a failed server must not see it."""


_call = threading.local()   # .epoch: the abort epoch this thread's summary began under


def abort_epoch():
    return _abort_epoch


def _begun():
    """The abort epoch the current call answers to: the one its summary was
    picked under, so an abandon() during the body fetch or a wait for the
    model still counts; otherwise the epoch right now."""
    epoch = getattr(_call, "epoch", None)
    return _abort_epoch if epoch is None else epoch


def _check_cancelled(epoch=None):
    """Raise if the reader has moved on since `epoch` (default: since this
    thread's call began)."""
    if (_begun() if epoch is None else epoch) != _abort_epoch:
        raise Cancelled("cancelled")


def abandon():
    """Stop the call in flight (generating, or waiting on a model load): the
    reader has moved on from its story, or wants it resubmitted."""
    global _abort_epoch
    with _health:
        _abort_epoch += 1
        _health.notify_all()


def retry_now():
    """The reader asked to resubmit: abandon whatever is in flight, forget
    recent failures, and re-probe the server."""
    abandon()
    _last_error.clear()
    _empty_replies.clear()
    note_demand()
    _wake.set()


def _await_ollama(target):
    """Hold a summary while the model loads, so the load can't use up the
    generation timeout; raise at once if Ollama can't serve it."""
    note_demand()
    with _health:
        epoch = _begun()
        _health.wait_for(
            lambda: epoch != _abort_epoch
            or (_ollama["target"] == target and _ollama["state"] not in _WAITING),
            timeout=config.OLLAMA_LOAD_TIMEOUT_SECONDS + 30)
        _check_cancelled(epoch)
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

# A summary doesn't need a reasoning pass, and reasoning costs several times
# the tokens of the summary itself. Ask for none; models that reason anyway
# (gpt-oss ignores "think": false) are asked for the least; models that reject
# the setting get no "think" field at all.
_THINK_LADDER = (False, "low", None)
_think_rung = {}       # model -> index of the first rung that works for it
_empty_replies = {}    # model -> consecutive empty replies at its current rung
MAX_REPLY_TOKENS = 3000   # reasoning included; stops a model that never finishes


def _ollama_chat(base_url, model, prompt, think, schema):
    """One reply -> (content, whether the model reasoned). Streamed so that a
    cancelled or overrunning generation stops at the next token instead of
    holding up every story behind it until the read timeout."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "format": schema,
        "options": {"num_predict": MAX_REPLY_TOKENS},
    }
    if think is not None:
        body["think"] = think
    epoch = _begun()
    _check_cancelled(epoch)
    deadline = time.time() + config.OLLAMA_TIMEOUT_SECONDS
    content, reasoned = [], False
    with requests.post(f"{base_url}/api/chat", json=body, stream=True,
                       timeout=(config.OLLAMA_CONNECT_TIMEOUT_SECONDS,
                                config.OLLAMA_TIMEOUT_SECONDS)) as resp:
        resp.raise_for_status()
        try:
            for line in resp.iter_lines():
                _check_cancelled(epoch)
                if time.time() > deadline:
                    raise requests.Timeout("timed out")
                # a stalled stream gets what is left of the deadline, not a
                # fresh read timeout on top of it
                _set_read_timeout(resp, max(1, deadline - time.time()))
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                message = chunk.get("message") or {}
                content.append(message.get("content") or "")
                reasoned = reasoned or bool(message.get("thinking"))
                if chunk.get("done"):
                    # cut off at the cap is not an empty reply: stepping up to
                    # more reasoning would only hit the cap again
                    if chunk.get("done_reason") == "length":
                        raise ReplyCutOff(reasoned)
                    return "".join(content), reasoned
        except requests.ConnectionError:
            if time.time() > deadline:
                raise requests.Timeout("timed out") from None
            raise
    # the server went away mid-reply: not a short or empty answer
    raise requests.ConnectionError("reply cut off")


class ReplyCutOff(RuntimeError):
    """The reply ran into MAX_REPLY_TOKENS; `reasoned` says whether the model
    spent tokens on reasoning."""

    def __init__(self, reasoned):
        super().__init__("reply hit the token limit")
        self.reasoned = reasoned


def _set_read_timeout(resp, seconds):
    try:
        resp.raw.connection.sock.settimeout(seconds)
    except Exception:
        pass   # no socket to reach: the request's own read timeout still applies


def _ollama_generate(base_url, model, prompt, schema=SUMMARY_SCHEMA):
    """Generate at the model's rung of the think ladder, stepping up when a
    rung is rejected (HTTP 400) or answers empty. A rejection (once a later
    rung is accepted: a 400 with another cause fails them all and settles
    nothing) or reasoning despite "think": false settles the rung at once; an
    empty reply can be a one-off, so it takes two in a row."""
    last = len(_THINK_LADDER) - 1
    try:
        with _ollama_serial:
            rung = _think_rung.get(model, 0)
            rejected = False   # a rung answered 400 during this call
            while True:
                try:
                    content, reasoned = _ollama_chat(
                        base_url, model, prompt, _THINK_LADDER[rung], schema)
                except requests.HTTPError as e:
                    if rung == last or e.response is None or e.response.status_code != 400:
                        raise
                    rung += 1
                    rejected = True
                    continue
                except ReplyCutOff as e:
                    if rung != 0 or not e.reasoned:
                        raise
                    # it reasoned through the cap despite "think": false: the
                    # next rung asks it for less, the same as when it answers
                    _think_rung[model] = rung = 1
                    continue
                if content.strip():
                    if rejected:   # the 400 was about "think": this rung was accepted
                        _think_rung[model] = rung
                    if rung == _think_rung.get(model, 0):
                        _empty_replies.pop(model, None)   # its own rung works
                    if rung == 0 and reasoned:
                        _think_rung[model] = 1
                    return content
                if rung == last:
                    return content
                _empty_replies[model] = _empty_replies.get(model, 0) + 1
                if _empty_replies[model] >= 2 and _think_rung.get(model, 0) == rung:
                    _think_rung[model] = rung + 1
                    _empty_replies.pop(model, None)
                rung += 1
    except requests.RequestException:
        _wake.set()   # re-probe: the server may be down or have dropped the model
        raise


def call_ollama(prompt):
    base_url, model = target = _ollama_target()
    _await_ollama(target)
    return _parse_summary(_ollama_generate(base_url, model, prompt))


_anthropic_unsaved = [None, 0]   # [UTC day, answered calls that day with no usable summary]


def _anthropic_calls_today():
    """Calls that count against the daily cap: saved summaries, plus replies
    that were paid for but couldn't be used."""
    day = store.utc_day()
    if _anthropic_unsaved[0] != day:
        _anthropic_unsaved[:] = [day, 0]
    return store.anthropic_calls_today() + _anthropic_unsaved[1]


def call_anthropic(prompt):
    api_key = config.setting("anthropic_api_key")
    if not api_key:
        raise RuntimeError("Anthropic API key not set")
    if _anthropic_calls_today() >= config.MAX_ANTHROPIC_DAILY_ARTICLES:
        raise RuntimeError("daily Anthropic article cap reached")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": config.setting("anthropic_model"),
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    try:
        reply = resp.json()
        if reply.get("stop_reason") == "max_tokens":
            raise RuntimeError("reply hit the token limit")
        return _parse_summary(reply["content"][0]["text"])
    except Exception:
        _anthropic_unsaved[1] += 1
        raise


_BACKENDS = {
    "ollama": (call_ollama, lambda: _ollama_target()[1]),
    "anthropic": (call_anthropic, lambda: config.setting("anthropic_model")),
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
    # equal roles keep the order above: the local server before the paid one
    return [name for _, name in sorted(ranked, key=lambda r: r[0])]


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
        "choice": "Another model is already loaded; waiting for you to pick one",
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
            "anthropic": {"state": claude, "detail": note},
            "offer": model_offer()}


# --- ticker (Ollama only, never routed through the fallback) ---

TICKER_SCHEMA = {
    "type": "object",
    "properties": {"ticker": {"type": "string"}},
    "required": ["ticker"],
}

TICKER_PROMPT = """You write lines for a scrolling news ticker, wire-service style.

HEADLINE: {headline}
DETAIL: {detail}

Rewrite it as one ticker line: at most 8 words, facts only, present tense, the
concrete subject and what happened. No teaser, no question, no trailing period.
Return ONLY JSON: {{"ticker": "..."}}"""

TICKER_MAX_WORDS = 8


def ticker_error():
    """Why the ticker can't condense right now, or None. The ticker uses Ollama
    only, so Claude's state never matters here."""
    base_url, model = target = _ollama_target()
    if config.setting("ollama_role") == "off":
        return "Ollama is off in settings; the ticker needs Ollama"
    with _health:
        state = _ollama["state"] if _ollama["target"] == target else "checking"
        message = _ollama["message"]
    if state == "unreachable":
        return f"Ollama unreachable at {base_url}"
    if state == "missing":
        return f"{model} isn't installed on {base_url} (ollama pull {model})"
    if state == "error":
        return f"Couldn't load {model}: {message}"
    if state == "choice":
        return "Another model is already loaded; open the reader to pick one"
    if state == "ready" and (err := _recent_error("ticker", time.time())):
        return f"Ticker line failed: {err}"
    return None


def condense_headline(article):
    """<=8-word ticker line from the headline (the straight one if the story
    has been summarized) and the feed's own description. No body fetch and no
    full summary, so the on-demand summary rule holds. Fails fast instead of
    waiting on a model load: the ticker retries on its next pass."""
    base_url, model = _ollama_target()
    if config.setting("ollama_role") == "off":
        raise RuntimeError("Ollama is off")
    note_demand()
    if not ollama_ready():
        raise RuntimeError("Ollama model not ready")
    prompt = TICKER_PROMPT.format(
        headline=article.get("straight_headline") or article.get("original_headline") or "",
        detail=re.sub(r"<[^>]+>", " ", article.get("rss_description") or "")[:300].strip()
               or "(none)")
    try:
        content = _ollama_generate(base_url, model, prompt, TICKER_SCHEMA)
        match = re.search(r"\{.*\}", content, re.S)
        line = json.loads(match.group(0))["ticker"] if match else ""
        line = re.sub(r"\s+", " ", str(line)).strip().rstrip(".")
        if not line:
            raise ValueError("empty ticker line")
    except Cancelled:
        raise
    except Exception as e:
        _last_error["ticker"] = (time.time(), _short_error(e))
        raise RuntimeError(f"[ticker] {e}") from e
    _last_error.pop("ticker", None)
    return " ".join(line.split()[:TICKER_MAX_WORDS])


def summarize(article, source_name):
    """Router: try servers in the user's primary/secondary order (spec §6.1).
    Returns (summary, provider, model) or raises if all enabled backends fail."""
    backends = enabled_backends()
    if not backends:
        raise RuntimeError("all AI servers are set to off")
    prompt = build_prompt(article, source_name)
    errors = []
    for provider in backends:
        fn, model_name = _BACKENDS[provider]
        try:
            summary = fn(prompt)
        except Cancelled:
            raise   # not a server failure: don't fall through to the next one
        except Exception as e:  # timeout, connection, malformed output
            _last_error[provider] = (time.time(), _short_error(e))
            errors.append(f"{provider}: {_short_error(e)}")
            continue
        _last_error.pop(provider, None)
        return summary, provider, model_name()
    raise RuntimeError("; ".join(errors))


def body_to_summarize(article_id):
    """The story's row with its text in place, or None if there is nothing to
    summarize (gone, or summarized meanwhile: its purged text isn't fetched
    again just to be thrown away). Raises ingest.SiteDown like ensure_body."""
    article = store.get_article(article_id)
    if not article or article["processed"]:
        return None
    return ingest.ensure_body(article_id)


def summarize_article(article_id, epoch=None):
    """Summarize one story, on demand only. Raises RuntimeError if every
    enabled server failed, or ingest.SiteDown while the story's site is down
    (better to try again than to summarize the feed's blurb); the card keeps
    its original headline meanwhile.
    `epoch` is abort_epoch() from when the story was picked: an abandon()
    since then cancels it wherever it has got to."""
    _call.epoch = epoch
    try:
        _check_cancelled()   # before waiting on a fetch already under way
        article = body_to_summarize(article_id)
        _check_cancelled()
        if not article or article["processed"]:
            return
        feeds = {f["id"]: f["name"] for f in store.get_feeds()}
        summary, provider, model = summarize(article, feeds.get(article["feed_source_id"]))
        store.save_summary(article_id, summary, provider, model)
    finally:
        _call.epoch = None
