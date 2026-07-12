"""SQLite persistence (spec §4, adapted from Postgres+Redis to a single-user desktop store).

body_text lives in a column with a TTL purge instead of Redis (spec §12: no
long-term full-text storage).
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import config

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rss_url TEXT NOT NULL UNIQUE,
    etag TEXT,
    last_modified TEXT,
    last_fetched TEXT,
    extraction_success_rate REAL DEFAULT 1.0,
    enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    feed_source_id TEXT REFERENCES feed_sources(id),
    canonical_url TEXT,
    dedup_hash TEXT UNIQUE,
    original_headline TEXT,
    rss_description TEXT,
    straight_headline TEXT,
    bluf_bullets TEXT,          -- JSON array
    one_sentence TEXT,
    body_text TEXT,
    extracted_at TEXT,
    ai_provider TEXT,
    ai_model TEXT,
    confidence_score REAL,
    pub_date TEXT,
    topic_tags TEXT,            -- JSON array
    is_paywalled INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    card_image_url TEXT,
    is_read INTEGER DEFAULT 0,
    ai_processed_at TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_queue ON articles (is_read, processed, pub_date);
"""


def _connect():
    global _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def execute(sql, params=()):
    with _lock:
        conn = _connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def query(sql, params=()):
    with _lock:
        conn = _connect()
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def now():
    return datetime.now(timezone.utc).isoformat()


def _story_age_cutoff():
    """ISO timestamp before which stories are considered dated and not offered."""
    try:
        days = int(config.setting("max_story_age_days"))
    except (TypeError, ValueError):
        days = 3
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# --- feed sources ---

def seed_default_feeds():
    if not query("SELECT id FROM feed_sources LIMIT 1"):
        for name, url in config.DEFAULT_FEEDS:
            add_feed(name, url)


def add_feed(name, rss_url):
    execute(
        "INSERT OR IGNORE INTO feed_sources (id, name, rss_url) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), name, rss_url),
    )


def remove_feed(feed_id):
    execute("DELETE FROM feed_sources WHERE id = ?", (feed_id,))


def set_feed_enabled(feed_id, enabled):
    execute("UPDATE feed_sources SET enabled = ? WHERE id = ?", (1 if enabled else 0, feed_id))


def get_feeds(enabled_only=False):
    sql = "SELECT * FROM feed_sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return query(sql + " ORDER BY name")


def update_feed_fetch_meta(feed_id, etag, last_modified):
    execute(
        "UPDATE feed_sources SET etag = ?, last_modified = ?, last_fetched = ? WHERE id = ?",
        (etag, last_modified, now(), feed_id),
    )


# --- articles ---

def insert_article(feed_source_id, canonical_url, dedup_hash, original_headline,
                   rss_description, pub_date):
    """INSERT ... ON CONFLICT DO NOTHING equivalent (spec §5.1). Returns True if new."""
    cur = execute(
        """INSERT OR IGNORE INTO articles
           (id, feed_source_id, canonical_url, dedup_hash, original_headline,
            rss_description, pub_date, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), feed_source_id, canonical_url, dedup_hash,
         original_headline, rss_description, pub_date, now()),
    )
    return cur.rowcount > 0


def articles_needing_extraction(limit=25):
    """Same priority as get_queue (newest day first, sources taking turns within
    a day) so the per-pass extraction budget goes to the stories offered next,
    and dated stories are never extracted at all."""
    return query(
        "SELECT * FROM ("
        "  SELECT *, substr(pub_date, 1, 10) AS day, ROW_NUMBER() OVER "
        "    (PARTITION BY feed_source_id, substr(pub_date, 1, 10) "
        "     ORDER BY pub_date DESC) AS source_rank "
        "  FROM articles WHERE extracted_at IS NULL AND is_read = 0 AND pub_date >= ?"
        ") ORDER BY day DESC, source_rank, pub_date DESC LIMIT ?",
        (_story_age_cutoff(), limit))


def save_extraction(article_id, body_text, is_paywalled, card_image_url):
    execute(
        "UPDATE articles SET body_text = ?, is_paywalled = ?, card_image_url = ?, "
        "extracted_at = ? WHERE id = ?",
        (body_text, 1 if is_paywalled else 0, card_image_url, now(), article_id),
    )


def save_summary(article_id, summary, provider, model):
    execute(
        """UPDATE articles SET straight_headline = ?, bluf_bullets = ?, one_sentence = ?,
           topic_tags = ?, confidence_score = ?, ai_provider = ?, ai_model = ?,
           processed = 1, ai_processed_at = ? WHERE id = ?""",
        (summary["straight_headline"], json.dumps(summary["bluf_bullets"]),
         summary["one_sentence"], json.dumps(summary["topic_tags"]),
         summary["confidence"], provider, model, now(), article_id),
    )


def anthropic_calls_today():
    day = datetime.now(timezone.utc).date().isoformat()
    rows = query(
        "SELECT COUNT(*) AS n FROM articles WHERE ai_provider = 'anthropic' "
        "AND ai_processed_at >= ?", (day,))
    return rows[0]["n"]


def purge_stale_bodies():
    """Redis-TTL equivalent: drop body text older than the cache TTL (spec §12)."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=config.BODY_TEXT_CACHE_TTL_HOURS)).isoformat()
    execute(
        "UPDATE articles SET body_text = NULL WHERE body_text IS NOT NULL "
        "AND extracted_at < ?", (cutoff,))


def _decode_card(r):
    r["bluf_bullets"] = json.loads(r["bluf_bullets"]) if r["bluf_bullets"] else []
    r["topic_tags"] = json.loads(r["topic_tags"]) if r["topic_tags"] else []
    r.pop("body_text", None)
    return r


def get_backlog(limit=100):
    """Unread stories older than the max-age window. Never offered in the main
    queue — the end-of-stack card reports the count and reveals them on demand."""
    cutoff = _story_age_cutoff()
    count = query(
        "SELECT COUNT(*) AS n FROM articles WHERE is_read = 0 AND pub_date < ?",
        (cutoff,))[0]["n"]
    items = []
    if limit:
        items = [_decode_card(r) for r in query(
            "SELECT a.*, f.name AS source_name FROM articles a "
            "LEFT JOIN feed_sources f ON f.id = a.feed_source_id "
            "WHERE a.is_read = 0 AND a.pub_date < ? "
            "ORDER BY a.pub_date DESC LIMIT ?", (cutoff, limit))]
    return {"count": count, "items": items}


def get_queue(limit=50):
    """Unread cards, newest day first (spec §4.3, adapted). Within a day the
    sources take turns — each source's newest story, then each one's
    second-newest, and so on — so a prolific feed can't crowd the others out
    while yesterday's news never outranks today's. Stories older than the
    max_story_age_days setting are not offered."""
    rows = query(
        "SELECT * FROM ("
        "  SELECT a.*, f.name AS source_name, "
        "         substr(a.pub_date, 1, 10) AS day, ROW_NUMBER() OVER "
        "    (PARTITION BY a.feed_source_id, substr(a.pub_date, 1, 10) "
        "     ORDER BY a.pub_date DESC) AS source_rank "
        "  FROM articles a "
        "  LEFT JOIN feed_sources f ON f.id = a.feed_source_id "
        "  WHERE a.is_read = 0 AND a.pub_date >= ?"
        ") ORDER BY day DESC, source_rank, pub_date DESC LIMIT ?",
        (_story_age_cutoff(), limit))
    for r in rows:
        _decode_card(r)
        r.pop("source_rank", None)
        r.pop("day", None)
    return rows


def get_article(article_id):
    rows = query("SELECT * FROM articles WHERE id = ?", (article_id,))
    return rows[0] if rows else None


def mark_read(article_id):
    execute("UPDATE articles SET is_read = 1 WHERE id = ?", (article_id,))


def status():
    q = query
    return {
        "unread": q("SELECT COUNT(*) AS n FROM articles WHERE is_read = 0")[0]["n"],
        "processed_unread": q(
            "SELECT COUNT(*) AS n FROM articles WHERE is_read = 0 AND processed = 1")[0]["n"],
        "total": q("SELECT COUNT(*) AS n FROM articles")[0]["n"],
        "by_provider": {r["ai_provider"] or "none": r["n"] for r in q(
            "SELECT ai_provider, COUNT(*) AS n FROM articles WHERE processed = 1 "
            "GROUP BY ai_provider")},
    }
