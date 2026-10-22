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

A story reported more than once gets one summary combined from its reports
(see stories.py), after Ollama is asked which nearby unread stories are the
same story. A story the AI finds no news in is hidden instead of summarized.
"""
import json
import re
import threading
import time
from datetime import datetime, timezone

import requests

from . import config, ingest, store, stories

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "straight_headline": {"type": "string"},
        "bluf_bullets": {"type": "array", "items": {"type": "string"},
                         "minItems": 3, "maxItems": 5},
        "one_sentence": {"type": "string"},
        "topic_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "no_news": {"type": "boolean"},
    },
    "required": ["straight_headline", "bluf_bullets", "one_sentence",
                 "topic_tags", "confidence", "no_news"],
}

_SUMMARY_FIELDS = """Use only what the text above says. This is current news, published on the date
given. Never add a year, date, age, number, or name from your own knowledge: if the text
says "this season" or "on Monday", say that, not a year.

Return a JSON object with:
- straight_headline: factual, declarative, <=16 words. State what happened AND the most
  important concrete detail: what the subject actually does, changes, decides, costs, or
  affects. Never leave the subject generic ("a new bill", "a major policy", "a tech company")
  when the body says specifically what it is or does. Example: not "New housing bill becomes
  law" but "Housing bill limiting investor purchases and easing modular-home rules becomes law".
  No teasers.
- bluf_bullets: 3-5 bullets, each a complete sentence with the key facts. Do not repeat the
  headline's detail verbatim; add the text's specifics (numbers, names, dates, consequences).
- one_sentence: the single most important fact in <=25 words.
- topic_tags: up to 4 topic labels.
- confidence: float 0-1 reflecting how well the body supports the summary.
- no_news: true if {no_news_when} only a podcast or video listing, a sponsor message, a
  quiz, a list of links, or a lone quote or question with no reporting. Otherwise false.
  When true, the other fields don't matter.

Return ONLY valid JSON. No preamble."""

PROMPT_TEMPLATE = """You are a news editor who rewrites clickbait headlines and produces factual summaries.

ARTICLE TITLE: {original_headline}
SOURCE: {source_name}
PUBLISHED: {published}
TODAY: {today}
ARTICLE BODY:
---
{body}
---

""" + _SUMMARY_FIELDS.replace("{no_news_when}", "there is no news to report: the text is")

COMBINED_PROMPT_TEMPLATE = """You are a news editor who rewrites clickbait headlines and produces factual summaries.

The {count} reports below cover the same story. Write ONE summary of the story as a whole:
what the reports have in common first, then the specifics only some of them give. Never
state the same fact twice. The headline covers the whole story, not one report's angle.

TODAY: {today}

{reports}

""" + _SUMMARY_FIELDS.replace("{no_news_when}", "none of the reports has news to report: each is")

_REPORT = """REPORT {n} ({source_name}, published {published}): {original_headline}
---
{body}
---"""

BODY_CHARS = 4000            # article text sent for one report
COMBINED_BODY_CHARS = 6000   # ...shared between the reports of a combined summary


def _text(article):
    return (article.get("body_text") or "").strip() or (article.get("rss_description") or "").strip()


def _day(when=None):
    """'Monday, October 19, 2026' in local time: a model left to guess the date
    assumes the year its training ended in."""
    try:
        t = datetime.fromisoformat(when) if when else datetime.now(timezone.utc)
    except ValueError:
        return "unknown"
    t = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone()
    return f"{t:%A, %B} {t.day}, {t.year}"


def _reports(article, source_name, others=()):
    """[(report, its text as the prompt sends it)]: the lead article, plus
    `others` (rows with source_name) reporting the same story."""
    if not others:
        return [(dict(article, source_name=source_name),
                 (article.get("body_text") or "")[:BODY_CHARS])]
    reports = [dict(article, source_name=source_name), *others]
    share = COMBINED_BODY_CHARS // len(reports)
    return [(r, _text(r)[:share]) for r in reports]


def build_prompt(article, source_name, others=()):
    """The summary prompt for a story: its lead article, plus `others` (rows
    with source_name) reporting the same story, combined into one summary."""
    reports = _reports(article, source_name, others)
    if len(reports) == 1:
        r, body = reports[0]
        return PROMPT_TEMPLATE.format(
            original_headline=r["original_headline"], source_name=source_name or "Unknown",
            published=_day(r.get("pub_date")), today=_day(), body=body)
    return COMBINED_PROMPT_TEMPLATE.format(count=len(reports), today=_day(), reports="\n\n".join(
        _REPORT.format(n=n, source_name=r.get("source_name") or "Unknown",
                       published=_day(r.get("pub_date")),
                       original_headline=r["original_headline"], body=body or "(no text)")
        for n, (r, body) in enumerate(reports, 1)))


# A model that finds nothing to summarize sometimes says so in the headline
# instead of setting no_news ("Unable to summarize: article body contains only
# a question"). It must also talk about the article itself (_META): "No
# details provided on suspect's motive" and "No information yet on Article 5"
# are news, and a match hides a story for good. Checked against all 3,636
# summaries LeadLine had made, it matched only the 8 that said so.
_NO_NEWS_HEADLINE = re.compile(
    r"^(?:unable|cannot|can't|could ?n[o']t)\b.{0,20}\b"
    r"(?:summari[sz]e|generate|determine|create|produce|extract|identify|write)"
    r"|^no (?:substantive |actual |real |specific |reportable )?"
    r"(?:news|story|article|content|information|details)\b"
    r"|\b(?:article|story)(?: body| text| content)? (?:is |was )?"
    r"(?:missing|empty|insufficient|unavailable|lacks?)\b"
    r"|\b(?:insufficient|no|missing) (?:article|news|story) (?:content|text|body|information|details)\b",
    re.I)


_META = re.compile(r"\barticles?\b(?!\s*\d)", re.I)   # not "Article 5"


def headline_says_no_news(headline, confidence):
    return (confidence <= 0.5 and bool(_NO_NEWS_HEADLINE.search(headline or ""))
            and bool(_META.search(headline or "")))


def redo_invented_summaries():
    """Unread summaries made before the date check that state a year or age
    their article doesn't are made again when read. Only those whose text is
    still kept can be checked. Returns how many."""
    redo = []
    for r in store.unread_summaries_with_text():
        summary = {"no_news": False, "straight_headline": r["straight_headline"] or "",
                   "one_sentence": r["one_sentence"] or "", "bluf_bullets": r["bluf_bullets"]}
        if _summary_invents(summary, _known([(r, r["body_text"] or "")])):
            redo.append(r["id"])
    store.unsummarize(redo)
    return len(redo)


def hide_no_news_summaries():
    """Hide summaries made before no_news existed whose headline says there
    was nothing to summarize. Returns how many."""
    found = [r["id"] for r in store.summarized_headlines()
             if headline_says_no_news(r["straight_headline"], r["confidence_score"] or 0)]
    for article_id in found:
        stories.drop_no_news(article_id)
    return len(found)


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _clean(text):
    """A sentence without JSON left over from a sloppy reply ('…2028.”,')."""
    s = str(text).strip().rstrip(",").rstrip()
    if s.endswith('"') and s.count('"') % 2:
        s = s[:-1].rstrip()
    if s.endswith("”") and s.count("“") < s.count("”"):
        s = s[:-1].rstrip()
    return s


_YEAR = re.compile(r"\b(?:19|20)\d\d\b")
_AGE = re.compile(r"\b(\d{1,3})[\s-]*years?[\s-]*old\b", re.I)
def _digits(text):
    """Numbers written one way, so a faithful rewording isn't taken for an
    invention: "2,000" is 2000, and odd dashes and spaces are plain ones.
    (Spelled-out numbers aren't read: news style writes ages in figures, and
    a miss costs one retry at most, never the summary.)"""
    text = re.sub(r"[\u00a0\u202f]", " ", re.sub(r"[\u2010-\u2015]", "-", text))
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)


def _year(when):
    try:
        t = datetime.fromisoformat(when)
    except (TypeError, ValueError):
        return None
    return str((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone().year)


def _known(reports):
    """(numbers, years) a summary may state: the numbers in the reports' own
    titles, descriptions and text, and the years they were published (and
    this one), which the prompt gives the model. Not the prompt's other
    digits: "19-year-old" isn't known because it is the 19th."""
    numbers = set()
    for r, body in reports:
        numbers.update(re.findall(r"\d+", _digits(" ".join(
            [r.get("original_headline") or "", r.get("rss_description") or "", body or ""]))))
    years = {_year(r.get("pub_date")) for r, _ in reports} | {str(datetime.now().year)}
    return numbers, years - {None}


def _invented(text, known):
    """Years and ages in `text` the articles never mention (`known`, from
    _known): a model's own guesses from its training days, like "the 2024
    season" in a 2026 story."""
    numbers, years = known
    text = _digits(text)
    return ({y for y in _YEAR.findall(text) if y not in numbers and y not in years}
            | {f"{n}-year-old" for n in _AGE.findall(text) if n not in numbers})


def _summary_invents(summary, known):
    if summary["no_news"]:
        return set()
    return _invented(" ".join([summary["straight_headline"], summary["one_sentence"],
                               *summary["bluf_bullets"]]), known)


def _without_invented(summary, known):
    """The summary minus the bullets that state a year or age the article
    doesn't, as long as one is left. It never fails: a story left with no
    summary would be retried, and paid for, again and again. So if every
    bullet states one, or the headline does, the summary stands as it is."""
    bullets = [b for b in summary["bluf_bullets"] if not _invented(b, known)]
    if not bullets:
        return summary
    one = summary["one_sentence"]
    return dict(summary, bluf_bullets=bullets,
                one_sentence=bullets[0] if _invented(one, known) else one)


def _parse_summary(text):
    """Validate model output against the ArticleSummary shape (spec §6.3).
    A story with no news comes back as {"no_news": True} and nothing else."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in response")
    data = json.loads(match.group(0))
    if _truthy(data.get("no_news")):
        return {"no_news": True}
    bullets = [_clean(b) for b in data["bluf_bullets"] if _clean(b)]
    headline = _clean(data["straight_headline"])
    confidence = min(1.0, max(0.0, float(data.get("confidence", 0.5))))
    if headline_says_no_news(headline, confidence):
        return {"no_news": True}
    if not 3 <= len(bullets) <= 5:
        raise ValueError("bluf_bullets must have 3-5 items")
    return {
        "straight_headline": headline,
        "bluf_bullets": bullets,
        "one_sentence": _clean(data["one_sentence"]),
        "topic_tags": [str(t).strip() for t in data.get("topic_tags", [])][:4],
        "confidence": confidence,
        "no_news": False,
    }


# --- is it the same story? (asked just before a story is summarized) ---

GROUP_SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "array", "items": {"type": "integer"}}},
    "required": ["same"],
}

GROUP_PROMPT = """You sort news for a reader who doesn't want to read the same story twice.

MAIN STORY: {headline}
{detail}

OTHER STORIES:
{candidates}

Which of the other stories are about the same thing as the main story: the same news event,
or the same specific product (the same car model, the same phone)? Sharing a company, a
person, a place, or a broad topic is not enough. A roundup, digest, podcast episode, or
deals post that covers several things is never the same story as anything.

Return ONLY JSON: {{"same": [numbers of the matching other stories]}}"""


def _brief(article, chars=200):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", article.get("rss_description") or ""))[:chars].strip()


def build_group_prompt(lead, members, candidates):
    also = "".join(f"\nAlso reported as: {m['original_headline']}" for m in members[:3])
    return GROUP_PROMPT.format(
        headline=lead["original_headline"],
        detail=(_brief(lead, 300) + also).strip() or "(no description)",
        candidates="\n".join(
            f"{n}. {c['original_headline']}" + (f" — {_brief(c)}" if _brief(c) else "")
            for n, c in enumerate(candidates, 1)))


def _parse_group(count):
    def parse(text):
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("no JSON object in response")
        same = json.loads(match.group(0)).get("same") or []
        if isinstance(same, (str, int, float)):   # "1, 3" or 2 rather than a list
            same = re.findall(r"\d+", str(same))
        picked = set()
        for n in same:
            try:
                n = int(float(n))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= count:
                picked.add(n)
        return sorted(picked)
    return parse


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


def call_ollama(prompt, schema=SUMMARY_SCHEMA, parse=_parse_summary):
    base_url, model = target = _ollama_target()
    _await_ollama(target)
    return parse(_ollama_generate(base_url, model, prompt, schema))


_anthropic_unsaved = [None, 0]   # [UTC day, paid replies that day that saved no summary]


def _anthropic_calls_today():
    """Calls that count against the daily cap: saved summaries, plus replies
    that were paid for but couldn't be used."""
    day = store.utc_day()
    if _anthropic_unsaved[0] != day:
        _anthropic_unsaved[:] = [day, 0]
    return store.anthropic_calls_today() + _anthropic_unsaved[1]


def call_anthropic(prompt, schema=None, parse=_parse_summary):
    """`schema` is for Ollama's structured output; Claude follows the prompt."""
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
        return parse(reply["content"][0]["text"])
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


def _route(prompt, schema, parse):
    """Router: try servers in the user's primary/secondary order (spec §6.1).
    Returns (result, provider, model) or raises if all enabled backends fail."""
    backends = enabled_backends()
    if not backends:
        raise RuntimeError("all AI servers are set to off")
    errors = []
    for provider in backends:
        fn, model_name = _BACKENDS[provider]
        try:
            result = fn(prompt, schema, parse)
        except Cancelled:
            raise   # not a server failure: don't fall through to the next one
        except Exception as e:  # timeout, connection, malformed output
            _last_error[provider] = (time.time(), _short_error(e))
            errors.append(f"{provider}: {_short_error(e)}")
            continue
        _last_error.pop(provider, None)
        return result, provider, model_name()
    raise RuntimeError("; ".join(errors))


_CORRECTION = """

Your last answer said {found}, which the text above never states. Use only what it says."""


def summarize(article, source_name, others=()):
    """The story's summary: of the article alone, or combined with `others`
    reporting the same story. Returns (summary, provider, model).
    A summary that states a year or age the article doesn't is asked for
    again, once; bullets that still do are dropped (see _without_invented)."""
    prompt = build_prompt(article, source_name, others)
    known = _known(_reports(article, source_name, others))
    first = _route(prompt, SUMMARY_SCHEMA, _parse_summary)
    found = _summary_invents(first[0], known)
    if not found:
        return first
    try:
        again = _route(prompt + _CORRECTION.format(found=", ".join(sorted(found))),
                       SUMMARY_SCHEMA, _parse_summary)
    except Cancelled:
        _count_unsaved(first)
        raise
    except Exception:
        again = None   # the first reply, trimmed, may still do
    if again:
        _count_unsaved(first)   # the retry replaces it
        if not _summary_invents(again[0], known):
            return again
    summary, provider, model = again or first
    return _without_invented(summary, known), provider, model


def _count_unsaved(result):
    """A Claude reply that won't be saved still counts against the daily cap:
    it was paid for. (A saved summary is counted from the database.)"""
    if result[1] == "anthropic":
        _anthropic_calls_today()   # starts a new day's count if the day changed
        _anthropic_unsaved[1] += 1


def _is_lead(article):
    """Still a card of its own: not folded into another story, and not found
    to have no news."""
    return not article["story_id"] and not article["no_news"]


def body_to_summarize(article_id):
    """The story's row with its text in place, or None if there is nothing to
    summarize (gone, summarized meanwhile — its purged text isn't fetched again
    just to be thrown away — or no longer a card of its own). Raises
    ingest.SiteDown like ensure_body."""
    article = store.get_article(article_id)
    if not article or article["processed"] or not _is_lead(article):
        return None
    return ingest.ensure_body(article_id)


def fetch_member_texts(article_id):
    """Fetch the text of the story's other reports, as far as their sites
    allow, for its combined summary. Nothing waits on this: a report whose
    text isn't in yet is summarized from its feed description."""
    for other in stories.summary_members(article_id):
        if other["body_text"] is None:
            try:
                ingest.ensure_body(other["id"], for_reader=False)
            except ingest.SiteDown:
                pass

_grouped = set()   # stories already asked about this session: a retry doesn't ask again


def fold_related(article, keep=()):
    """Put the unread stories that come close to this one to the AI, and fold
    the ones it says are the same story into this one (never one in `keep`).
    Asked of Ollama only, never Claude, like the ticker: those stories aren't
    the one being read, so their headlines go no further than the reader's own
    server. With Ollama off, not loaded or failing, they are left as they are.
    Cancelled is raised."""
    if article["id"] in _grouped:
        return
    candidates = stories.fold_candidates(article, keep)
    if candidates:
        if config.setting("ollama_role") == "off" or not ollama_ready():
            return   # asked again if this story's summary has to be retried
        prompt = build_group_prompt(article, stories.enabled_members(article["id"]), candidates)
        try:
            same = call_ollama(prompt, GROUP_SCHEMA, _parse_group(len(candidates)))
        except Cancelled:
            raise
        except Exception as e:
            print(f"[stories] same-story check failed: {e}")
            return   # asked again if this story's summary has to be retried
        _check_cancelled()   # the reader moved on while it was asked: ask again later
        stories.fold(article["id"], [candidates[n - 1]["id"] for n in same])
    _grouped.add(article["id"])


def summarize_article(article_id, epoch=None, keep=()):
    """Summarize one story, on demand only: its lead article combined with the
    other reports of it. A story the AI finds no news in is hidden instead.
    Raises RuntimeError if every enabled server failed, or ingest.SiteDown
    while the story's site is down (better to try again than to summarize the
    feed's blurb); the card keeps its original headline meanwhile.
    `epoch` is abort_epoch() from when the story was picked: an abandon()
    since then cancels it wherever it has got to. `keep`: stories that must
    not be folded into this one (the card on screen)."""
    _call.epoch = epoch
    try:
        _check_cancelled()   # before waiting on a fetch already under way
        article = body_to_summarize(article_id)
        _check_cancelled()
        if not article or article["processed"] or not _is_lead(article):
            return
        fold_related(article, keep)
        _check_cancelled()
        others = stories.summary_members(article_id)
        if not (article["body_text"] or "").strip() and not any(_text(o) for o in others):
            # neither the page nor the feed gave any text: a summary would be
            # invented from the headline
            raise RuntimeError("no article text to summarize")
        feeds = {f["id"]: f["name"] for f in store.get_feeds()}
        summary, provider, model = summarize(
            article, feeds.get(article["feed_source_id"]), others)
        if summary["no_news"]:   # none of the reports it read has any
            stories.drop_no_news(article_id, provider, model, also=[o["id"] for o in others])
        else:
            store.save_summary(article_id, summary, provider, model)
    finally:
        _call.epoch = None
