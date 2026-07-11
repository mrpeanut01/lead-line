"""AI processing layer (spec §6): Ollama primary, Anthropic Haiku fallback.

Both backends get the same prompt and must return the ArticleSummary JSON
shape. Provider used is logged per article. A hard daily cap guards the
Anthropic path (spec §10).
"""
import json
import re

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
- straight_headline: factual, declarative, <=12 words. State what happened. No teasers.
- bluf_bullets: 3-5 bullets, each a complete sentence with the key facts.
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


def call_ollama(prompt):
    resp = requests.post(
        f"{config.OLLAMA_BASE_URL}/api/chat",
        json={
            "model": config.OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": SUMMARY_SCHEMA,
        },
        timeout=config.OLLAMA_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return _parse_summary(resp.json()["message"]["content"])


def call_anthropic(prompt):
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    if store.anthropic_calls_today() >= config.MAX_ANTHROPIC_DAILY_ARTICLES:
        raise RuntimeError("daily Anthropic article cap reached")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return _parse_summary(resp.json()["content"][0]["text"])


def summarize(article, source_name):
    """Router: Ollama first, Anthropic on failure (spec §6.1). Returns
    (summary, provider, model) or raises if both fail."""
    prompt = build_prompt(article, source_name)
    errors = []
    for fn, provider, model in [
        (call_ollama, "ollama", config.OLLAMA_MODEL),
        (call_anthropic, "anthropic", config.ANTHROPIC_MODEL),
    ]:
        try:
            return fn(prompt), provider, model
        except Exception as e:  # timeout, connection, malformed output
            errors.append(f"[{provider}] {e}")
    raise RuntimeError("; ".join(errors))


def process_pending(limit=10):
    done = 0
    for article in store.articles_needing_ai(limit):
        feed = next((f for f in store.get_feeds() if f["id"] == article["feed_source_id"]), None)
        try:
            summary, provider, model = summarize(article, feed["name"] if feed else None)
        except RuntimeError:
            continue  # card falls back to original headline until a later pass
        store.save_summary(article["id"], summary, provider, model)
        done += 1
    return done
