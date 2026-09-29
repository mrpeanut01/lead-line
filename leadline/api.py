"""pywebview JS bridge — the UI's REST-equivalent surface."""
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path

import requests

from . import ai, config, ingest, store

if getattr(sys, "frozen", False):  # PyInstaller bundle
    _UI_DIR = Path(sys._MEIPASS) / "leadline" / "ui"
else:
    _UI_DIR = Path(__file__).parent / "ui"

SUMMARY_RETRY_SECONDS = 30   # after every server failed a story, wait before retrying it


class Api:
    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._summarize_lock = threading.Lock()
        self._inflight = set()
        self._failed = {}    # article id -> time its summary last failed
        self._ticker_lock = threading.Lock()
        self._ticker_busy = False
        self._ticker_failed = {}   # article id -> time its ticker line last failed
        self._window = None        # set by app.main(); private so JS can't reach it
        self._reader_geometry = None

    # --- queue / cards ---

    def get_queue(self, limit=50):
        return store.get_queue(limit)

    def get_backlog(self, limit=100):
        """Older unread stories hidden by max_story_age (limit=0: count only)."""
        return store.get_backlog(limit)

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

    def get_card(self, article_id):
        """A single story's card, e.g. one picked from the ticker tape."""
        return store.get_card(article_id)

    def mark_read(self, article_id):
        store.mark_read(article_id)
        return True

    def request_summaries(self, article_ids):
        """Summarize the read-ahead window in the background. Stories are only
        summarized on demand — never pre-processed en masse. The UI re-asks for
        stories still pending, so ones that just failed are skipped for a bit."""
        now = time.time()
        with self._summarize_lock:
            ids = [i for i in article_ids if i not in self._inflight
                   and now - self._failed.get(i, 0) > SUMMARY_RETRY_SECONDS]
            self._inflight.update(ids)
        if not ids:
            return {"queued": 0}
        ai.note_demand()

        def run():
            failed = ids
            try:
                failed = ai.summarize_articles(ids)
            finally:
                with self._summarize_lock:
                    self._inflight.difference_update(ids)
                    for i in ids:
                        self._failed.pop(i, None)
                    self._failed.update(dict.fromkeys(failed, time.time()))

        threading.Thread(target=run, daemon=True).start()
        return {"queued": len(ids)}

    # --- ticker view (Ollama only) ---

    def get_ticker(self):
        """The newest stories for the ticker tape. Only stories Ollama has
        condensed are returned; the rest are condensed in the background, one
        at a time, and show up on a later call. Never blocks on the model."""
        count = config.setting("ticker_count")
        rows = store.get_ticker(count, config.setting("ticker_max_age_hours"))
        now = time.time()
        with self._ticker_lock:
            todo = [r for r in rows if not r["ticker_headline"]
                    and now - self._ticker_failed.get(r["id"], 0) > SUMMARY_RETRY_SECONDS]
            start = bool(todo) and not self._ticker_busy
            if start:
                self._ticker_busy = True
        if start:
            threading.Thread(target=self._condense, args=(todo,), daemon=True).start()
        return {
            "items": [{"id": r["id"], "text": r["ticker_headline"],
                       "source_name": r["source_name"], "pub_date": r["pub_date"],
                       "canonical_url": r["canonical_url"]}
                      for r in rows if r["ticker_headline"]],
            "pending": sum(1 for r in rows if not r["ticker_headline"]),
            "ollama": ai.backend_status()["ollama"]["state"],
            "error": ai.ticker_error(),
        }

    def _condense(self, rows):
        try:
            for r in rows:   # newest first, so the top of the tape fills first
                if not ai.ollama_ready():
                    ai.note_demand()
                    break        # model not loaded yet; the next call starts over
                try:
                    store.save_ticker_headline(r["id"], ai.condense_headline(r))
                except RuntimeError:
                    with self._ticker_lock:
                        self._ticker_failed[r["id"]] = time.time()
        finally:
            with self._ticker_lock:
                self._ticker_busy = False

    def enter_ticker(self, height):
        """Shrink the window to a strip; remember the reader's size to restore."""
        w = self._window
        if not w:
            return False
        self._reader_geometry = (w.width, w.height)
        w.on_top = bool(config.setting("ticker_on_top"))
        w.resize(max(w.width, 640), int(height))
        return True

    def exit_ticker(self):
        w = self._window
        if not w:
            return False
        w.on_top = False
        width, height = self._reader_geometry or (540, 900)
        # resize keeps the top-left fixed; no move(), whose coordinates are
        # monitor-relative on some platforms while x/y are absolute
        w.resize(max(width, 420), max(height, 640))
        return True

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
        settings = config.save_settings(updates)
        with self._summarize_lock:
            self._failed.clear()   # a fixed key or server should retry right away
        with self._ticker_lock:
            self._ticker_failed.clear()
        ai.settings_changed()
        return settings

    def get_ai_status(self):
        """Ollama / Claude state for the status pill; in-memory, no network."""
        return ai.backend_status()

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
            with self._poll_lock:
                new = ingest.poll_all_feeds()
            ingest.extract_pending()
            store.purge_stale_bodies()
            return {"running": False, "new": new}
        finally:
            self._refresh_lock.release()

    def get_latest(self):
        """⟳: poll every feed now so the reader can jump back to the top with
        whatever is new. Poll only — extraction runs on the pipeline schedule
        and summaries on demand — so the button answers in seconds. If a
        scheduled poll is mid-flight, wait for it rather than skip."""
        with self._poll_lock:
            return {"new": ingest.poll_all_feeds()}

    def get_status(self):
        return store.status()
