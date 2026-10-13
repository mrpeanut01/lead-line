"""pywebview JS bridge — the UI's REST-equivalent surface."""
import json
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import requests

from . import ai, config, ingest, store

if getattr(sys, "frozen", False):  # PyInstaller bundle
    _UI_DIR = Path(sys._MEIPASS) / "leadline" / "ui"
else:
    _UI_DIR = Path(__file__).parent / "ui"

SUMMARY_RETRY_SECONDS = 30   # after every server failed a story, wait before retrying it
SUMMARY_RETRY_MAX_SECONDS = 600   # the wait doubles each time the same story fails again


class Api:
    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._work = threading.Condition()
        self._wanted = []    # stories the reader is waiting on, most urgent first
        self._working = None   # the one being summarized right now
        self._wanted_seq = 0   # newest request_summaries applied; late older ones are ignored
        self._done = set()   # wanted stories finished since the UI last caught up
        self._failed = {}    # article id -> (time its summary last failed, why, wait before retry)
        self._retry_gen = 0  # bumped by retry_summaries; older failures don't count
        self._bodies = {}    # article id -> Future fetching its text ahead of its turn
        self._fetch_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="body")
        threading.Thread(target=self._summary_worker, daemon=True).start()
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

    def request_summaries(self, article_ids, seq=None):
        """The stories the reader is waiting on, most urgent first: the one on
        screen, then the read-ahead window. Each call replaces the last, so a
        story scrolled past stops costing model time and the one on screen is
        never stuck behind it. Stories are only summarized on demand — never
        pre-processed en masse. Returns each story's state for its card. `seq`
        orders the UI's calls: one that arrives after a newer one changes nothing,
        and the reply carries the newest seq so a page that fell behind catches up."""
        asked = list(dict.fromkeys(article_ids))
        with self._work:
            if seq is not None:
                if seq <= self._wanted_seq:
                    return {"queued": len(self._wanted), "seq": self._wanted_seq,
                            "states": self._summary_states(asked)}
                self._wanted_seq = seq
            self._done.intersection_update(asked)
            ids = [i for i in asked if i not in self._done]
            new = [i for i in ids if i not in self._wanted]
            self._wanted = ids
            for i in [i for i in self._failed if i not in ids]:
                del self._failed[i]
            # scrolled past: forget its fetch, and unqueue one that hasn't begun
            for i in [i for i, f in self._bodies.items()
                      if i not in ids and (f.done() or f.cancel())]:
                del self._bodies[i]
            for i in ids:   # article text downloads while earlier stories summarize
                if i not in self._bodies:
                    self._bodies[i] = self._fetch_pool.submit(ai.ensure_body, i)
            self._work.notify_all()
            stale = self._working is not None and self._working not in ids
            states = self._summary_states(asked)
        if stale:
            ai.abandon()   # the story being summarized was scrolled past
        if new:
            ai.note_demand()
        return {"queued": len(ids), "seq": seq, "states": states}

    def retry_summaries(self, article_ids, seq=None):
        """↻ on a waiting card: abandon whatever is in flight, forget recent
        failures, re-probe the AI servers, and resubmit these stories now."""
        with self._work:
            self._retry_gen += 1
            self._failed.clear()
            self._bodies = {i: f for i, f in self._bodies.items() if not f.done()}
        with self._ticker_lock:
            self._ticker_failed.clear()
        ai.retry_now()
        return self.request_summaries(article_ids, seq)

    def _summary_states(self, ids):
        now = time.time()
        states = {}
        for i in ids:
            failed_at, why, wait = self._failed.get(i, (0, None, 0))
            if i in self._done:
                states[i] = {"state": "done"}   # the UI has yet to pick it up
            elif i == self._working:
                states[i] = {"state": "working"}
            elif now - failed_at < wait:
                states[i] = {"state": "failed", "error": why}
            else:
                states[i] = {"state": "queued"}
        return states

    def _next_summary(self):
        """Block until a wanted story is due (not inside its retry delay)."""
        with self._work:
            while True:
                now = time.time()
                waits = [wait - (now - failed_at) for failed_at, _, wait in
                         (self._failed.get(i, (0, None, 0)) for i in self._wanted)]
                due = [i for i, w in zip(self._wanted, waits) if w <= 0]
                if due:
                    self._working = due[0]
                    # the epoch is read here, under the lock that decides a
                    # story is stale, so no abandon() of it can be missed
                    return (due[0], self._retry_gen, self._bodies.get(due[0]),
                            ai.abort_epoch())
                self._work.wait(min(waits) if waits else None)

    def _summary_worker(self):
        """One story at a time, always the most urgent one the reader is
        waiting on at that moment."""
        while True:
            article_id, gen, body, epoch = self._next_summary()
            error = None
            try:
                waited = time.time()
                while body and ai.abort_epoch() == epoch and time.time() - waited < 60:
                    try:
                        body.result(timeout=1)
                    except FutureTimeout:
                        continue
                    except Exception:
                        pass   # summarize_article fetches it itself
                    break
                ai.summarize_article(article_id, epoch)
            except ai.Cancelled:
                error = ""     # resubmitted: goes again at once
            except Exception as e:
                error = (str(e) or type(e).__name__)[:200]
            with self._work:
                self._working = None
                if error is None:
                    self._failed.pop(article_id, None)
                    if article_id in self._wanted:
                        self._wanted.remove(article_id)
                        self._done.add(article_id)
                elif error and gen == self._retry_gen:
                    again = self._failed.get(article_id)
                    wait = (min(again[2] * 2, SUMMARY_RETRY_MAX_SECONDS) if again
                            else SUMMARY_RETRY_SECONDS)
                    self._failed[article_id] = (time.time(), error, wait)

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
                if self._wanted:
                    break        # the reader is waiting on a summary; it goes first
                try:
                    store.save_ticker_headline(r["id"], ai.condense_headline(r))
                except ai.Cancelled:
                    pass         # interrupted, not failed: the next pass redoes it
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
        with self._work:
            self._failed.clear()   # a fixed key or server should retry right away
            self._work.notify_all()
        with self._ticker_lock:
            self._ticker_failed.clear()
        ai.settings_changed()
        return settings

    def get_ai_status(self):
        """Ollama / Claude state for the status pill; in-memory, no network."""
        return ai.backend_status()

    def choose_ollama_model(self, model):
        """Answer the launch question: use an already-loaded model this session,
        or load the configured one."""
        ai.choose_model(model)
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
