"""pywebview JS bridge — the UI's REST-equivalent surface."""
import threading
import webbrowser

from . import ai, ingest, store


class Api:
    def __init__(self):
        self._refresh_lock = threading.Lock()

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

    # --- pipeline ---

    def refresh(self):
        """Run one full pipeline pass now (poll -> extract -> summarize -> purge)."""
        if not self._refresh_lock.acquire(blocking=False):
            return {"running": True}
        try:
            new = ingest.poll_all_feeds()
            ingest.extract_pending()
            ai.process_pending(limit=25)
            store.purge_stale_bodies()
            return {"running": False, "new": new}
        finally:
            self._refresh_lock.release()

    def get_status(self):
        return store.status()
