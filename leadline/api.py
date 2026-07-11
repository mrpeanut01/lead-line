"""pywebview JS bridge — the UI's REST-equivalent surface."""
import json
import sys
import threading
import webbrowser
from pathlib import Path

import requests

from . import ai, config, ingest, store

if getattr(sys, "frozen", False):  # PyInstaller bundle
    _UI_DIR = Path(sys._MEIPASS) / "leadline" / "ui"
else:
    _UI_DIR = Path(__file__).parent / "ui"


class Api:
    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._summarize_lock = threading.Lock()
        self._inflight = set()

    # --- queue / cards ---

    def get_queue(self, limit=50):
        return store.get_queue(limit)

    def get_body(self, article_id):
        """Body for inline expanded reading mode; re-extract if TTL-purged."""
        article = store.get_article(article_id)
        if not article:
            return {"body": ""}
        if not article["body_text"]:
            ingest.extract_article(article)
            article = store.get_article(article_id)
        return {"body": article["body_text"] or "",
                "is_paywalled": bool(article["is_paywalled"])}

    def mark_read(self, article_id):
        store.mark_read(article_id)
        return True

    def request_summaries(self, article_ids):
        """Summarize the read-ahead window in the background. Stories are only
        summarized on demand — never pre-processed en masse."""
        with self._summarize_lock:
            ids = [i for i in article_ids if i not in self._inflight]
            self._inflight.update(ids)
        if not ids:
            return {"queued": 0}

        def run():
            try:
                ai.summarize_articles(ids)
            finally:
                with self._summarize_lock:
                    self._inflight.difference_update(ids)

        threading.Thread(target=run, daemon=True).start()
        return {"queued": len(ids)}

    def open_source(self, url):
        """Source link opens the publisher in the system browser (spec §2)."""
        if url and url.startswith(("http://", "https://")):
            webbrowser.open(url)
        return True

    # --- feeds ---

    def get_feeds(self):
        return store.get_feeds()

    def add_feed(self, name, rss_url):
        store.add_feed(name.strip(), rss_url.strip())
        threading.Thread(target=self.refresh, daemon=True).start()
        return store.get_feeds()

    def remove_feed(self, feed_id):
        store.remove_feed(feed_id)
        return store.get_feeds()

    def set_feed_enabled(self, feed_id, enabled):
        store.set_feed_enabled(feed_id, enabled)
        return store.get_feeds()

    def get_catalog(self):
        """Bundled feed directory (plenaryapp/awesome-rss-feeds snapshot)."""
        try:
            return json.loads((_UI_DIR / "catalog.json").read_text())
        except (OSError, ValueError):
            return {"topics": [], "countries": []}

    # --- settings ---

    def get_settings(self):
        return config.load_settings()

    def save_settings(self, updates):
        return config.save_settings(updates)

    def discover_ollama_models(self, base_url=None):
        """List models available on the Ollama server (GET /api/tags)."""
        url = (base_url or config.setting("ollama_base_url")).rstrip("/")
        try:
            resp = requests.get(f"{url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = sorted(m["name"] for m in resp.json().get("models", []))
            return {"models": models, "error": None}
        except Exception:
            return {"models": [], "error": f"Ollama unreachable at {url}"}

    def discover_anthropic_models(self, api_key=None):
        """List models available to the given Anthropic key (GET /v1/models)."""
        key = api_key or config.setting("anthropic_api_key")
        if not key:
            return {"models": [], "error": "No API key set"}
        try:
            resp = requests.get(
                "https://api.anthropic.com/v1/models?limit=100",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
            if resp.status_code == 401:
                return {"models": [], "error": "Invalid API key"}
            resp.raise_for_status()
            return {"models": [m["id"] for m in resp.json().get("data", [])], "error": None}
        except Exception as e:
            return {"models": [], "error": f"Anthropic error: {type(e).__name__}"}

    # --- pipeline ---

    def refresh(self):
        """Run one pipeline pass now (poll -> extract -> purge). Summarization
        is NOT done here; it happens on demand via request_summaries."""
        if not self._refresh_lock.acquire(blocking=False):
            return {"running": True}
        try:
            new = ingest.poll_all_feeds()
            ingest.extract_pending()
            store.purge_stale_bodies()
            return {"running": False, "new": new}
        finally:
            self._refresh_lock.release()

    def get_status(self):
        return store.status()
