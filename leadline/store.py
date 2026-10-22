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
    created_at TEXT,
    ticker_headline TEXT,       -- <=8-word wire line for the ticker view (Ollama only)
    story_id TEXT,              -- the lead article of the story this one reports; NULL: a lead
    no_news INTEGER DEFAULT 0   -- the AI found no news in it (a podcast listing, a quiz...)
);
CREATE INDEX IF NOT EXISTS idx_articles_queue ON articles (is_read, processed, pub_date);
"""
# Columns added since the first release, for databases made before them.
_ADDED_COLUMNS = (("ticker_headline", "TEXT"), ("story_id", "TEXT"),
                  ("no_news", "INTEGER DEFAULT 0"))

# One story per canonical URL. Earlier releases keyed dedup on a hash that
# included the pub date, so feeds with missing/edited/future-dated timestamps
# re-inserted the same story every poll; collapse those duplicates (preferring
# the read copy, so seen stories don't resurrect as unread) before enforcing
# uniqueness. Idempotent — runs on every connect.
DEDUP_MIGRATION = """
DELETE FROM articles WHERE id NOT IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY canonical_url ORDER BY is_read DESC, created_at
        ) AS rn FROM articles
    ) WHERE rn = 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_canonical ON articles (canonical_url);
"""


def _connect():
    global _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.executescript(DEDUP_MIGRATION)
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(articles)")}
        for name, kind in _ADDED_COLUMNS:
            if name not in cols:
                _conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {kind}")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_story ON articles (story_id)")
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


def _batch(statements):
    """Several writes as one transaction."""
    with _lock:
        conn = _connect()
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def data_version():
    """Which one-time data upgrades have run (SQLite's user_version)."""
    return query("PRAGMA user_version")[0]["user_version"]


def set_data_version(version):
    execute(f"PRAGMA user_version = {int(version)}")


def _marks(ids):
    return ",".join("?" * len(ids))


def now():
    return datetime.now(timezone.utc).isoformat()


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


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
                   rss_description, pub_date, story_id=None, is_read=False):
    """INSERT ... ON CONFLICT DO NOTHING equivalent (spec §5.1). Returns the new
    article's id, or None if it was already there. story_id: the lead of the
    story it reports, if not a story of its own."""
    article_id = str(uuid.uuid4())
    cur = execute(
        """INSERT OR IGNORE INTO articles
           (id, feed_source_id, canonical_url, dedup_hash, original_headline,
            rss_description, pub_date, created_at, story_id, is_read)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (article_id, feed_source_id, canonical_url, dedup_hash,
         original_headline, rss_description, pub_date, now(), story_id, 1 if is_read else 0),
    )
    return article_id if cur.rowcount > 0 else None


def article_exists(canonical_url, dedup_hash):
    return bool(query("SELECT 1 FROM articles WHERE canonical_url = ? OR dedup_hash = ? LIMIT 1",
                      (canonical_url, dedup_hash)))


def is_read(article_id):
    rows = query("SELECT is_read FROM articles WHERE id = ?", (article_id,))
    return bool(rows and rows[0]["is_read"])


# --- stories: a lead article (story_id NULL) and the articles reporting it ---

def grouping_rows(since):
    """Headlines since `since` to group new articles against: from enabled
    feeds, no-news ones left out."""
    return query(
        "SELECT a.id, a.feed_source_id, a.original_headline, a.rss_description, a.pub_date, "
        "a.story_id, a.is_read, a.processed, length(a.body_text) AS body_len FROM articles a "
        "JOIN feed_sources f ON f.id = a.feed_source_id "
        "WHERE a.pub_date >= ? AND a.no_news = 0 AND f.enabled = 1", (since,))


def current_lead(article_id):
    """The lead of the story the article is in now (stories merge, and leads
    change), as a row with is_read and processed; None if that story is gone
    or has no news."""
    for _ in range(4):
        rows = query("SELECT id, story_id, no_news, is_read, processed FROM articles WHERE id = ?",
                     (article_id,))
        if not rows:
            return None
        if not rows[0]["story_id"]:
            return None if rows[0]["no_news"] else rows[0]
        article_id = rows[0]["story_id"]
    return None


def story_members(lead_id):
    """The story's other articles, without the lead and the ones with no news."""
    return query(
        "SELECT a.*, f.name AS source_name, COALESCE(f.enabled, 0) AS feed_enabled "
        "FROM articles a LEFT JOIN feed_sources f ON f.id = a.feed_source_id "
        "WHERE a.story_id = ? AND a.no_news = 0 ORDER BY a.pub_date", (lead_id,))


def leads_from_feed(feed_id):
    """Leads from this feed of stories other articles also report."""
    return [r["id"] for r in query(
        "SELECT DISTINCT l.id FROM articles l JOIN articles a ON a.story_id = l.id "
        "WHERE l.feed_source_id = ? AND l.story_id IS NULL", (feed_id,))]


def unread_story_leads(since, until, exclude):
    """Unread, unsummarized stories from enabled feeds published in [since, until]."""
    return query(
        "SELECT a.*, f.name AS source_name FROM articles a "
        "JOIN feed_sources f ON f.id = a.feed_source_id "
        "WHERE a.story_id IS NULL AND a.no_news = 0 AND a.is_read = 0 AND a.processed = 0 "
        "AND f.enabled = 1 AND a.pub_date BETWEEN ? AND ? AND a.id != ?",
        (since, until, exclude))


def join_story(lead_id, article_ids, unread_only=False):
    """Make these stories (their leads, and the articles reporting them) part
    of lead_id's story; if the reader has read any of it, it is all read.
    Checked as one transaction, so a story that changed meanwhile is left
    alone: the lead must still lead a story with news in it, and each of the
    others must still be a lead too (unread and unsummarized, with
    unread_only — as must the lead). Returns how many stories joined."""
    if not article_ids:
        return 0
    with _lock:
        conn = _connect()
        try:
            lead = conn.execute("SELECT story_id, no_news, is_read FROM articles WHERE id = ?",
                                (lead_id,)).fetchone()
            if not lead or lead["story_id"] or lead["no_news"] or (unread_only and lead["is_read"]):
                return 0
            still = " AND is_read = 0 AND processed = 0" if unread_only else ""
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM articles WHERE id IN ({_marks(article_ids)}) AND id != ? "
                f"AND story_id IS NULL{still}", (*article_ids, lead_id))]
            if ids:
                marks = _marks(ids)
                conn.execute(f"UPDATE articles SET story_id = ? WHERE (id IN ({marks}) "
                             f"OR story_id IN ({marks})) AND id != ?", (lead_id, *ids, *ids, lead_id))
                conn.execute(
                    "UPDATE articles SET is_read = 1 WHERE (id = ? OR story_id = ?) AND EXISTS ("
                    "  SELECT 1 FROM articles WHERE (id = ? OR story_id = ?) AND is_read = 1 "
                    "  AND no_news = 0)", (lead_id,) * 4)
            conn.commit()
            return len(ids)
        except Exception:
            conn.rollback()
            raise


def set_lead(lead_id, article_ids):
    """The story of these articles is led by lead_id from now on."""
    others = [i for i in article_ids if i != lead_id]
    statements = [("UPDATE articles SET story_id = NULL WHERE id = ?", (lead_id,))]
    if others:
        statements.append((f"UPDATE articles SET story_id = ? WHERE id IN ({_marks(others)})",
                           (lead_id, *others)))
    _batch(statements)


def carry_summary(from_id, to_id):
    """A story's new lead takes over the summary its old lead had (it is the
    story's summary, not one report's), so the story isn't summarized, and
    paid for, again. Not ai_processed_at: no new call was made."""
    execute(
        "UPDATE articles SET (straight_headline, bluf_bullets, one_sentence, topic_tags, "
        "confidence_score, ai_provider, ai_model, processed) = ("
        "  SELECT straight_headline, bluf_bullets, one_sentence, topic_tags, "
        "  confidence_score, ai_provider, ai_model, 1 FROM articles WHERE id = ?) "
        "WHERE id = ? AND processed = 0 AND EXISTS ("
        "  SELECT 1 FROM articles WHERE id = ? AND processed = 1 AND no_news = 0)",
        (from_id, to_id, from_id))


def mark_no_news(article_id, provider=None, model=None):
    """The AI found nothing to report in it: never offered, never summarized
    again. provider/model: the call that found it (None: an earlier summary)."""
    if provider is None:
        execute("UPDATE articles SET no_news = 1, processed = 1 WHERE id = ?", (article_id,))
        return
    execute(
        "UPDATE articles SET no_news = 1, processed = 1, ai_provider = ?, ai_model = ?, "
        "ai_processed_at = ? WHERE id = ?", (provider, model, now(), article_id))


def summarized_headlines():
    return query("SELECT id, straight_headline, confidence_score FROM articles "
                 "WHERE processed = 1 AND no_news = 0")


def unread_summaries_with_text():
    """Unread summaries whose article text is still kept, bullets decoded."""
    rows = query("SELECT * FROM articles WHERE processed = 1 AND no_news = 0 AND is_read = 0 "
                 "AND body_text IS NOT NULL AND body_text != ''")
    for r in rows:
        r["bluf_bullets"] = json.loads(r["bluf_bullets"]) if r["bluf_bullets"] else []
    return rows


def unsummarize(article_ids):
    """Drop these summaries: each is made again when its story is read."""
    if article_ids:
        execute("UPDATE articles SET processed = 0, straight_headline = NULL, bluf_bullets = NULL, "
                f"one_sentence = NULL WHERE id IN ({_marks(article_ids)})", tuple(article_ids))


def hidden_reasons(article_ids):
    """Of these stories, the ones no longer offered on a card of their own:
    id -> 'no_news', or 'merged' (now part of another story)."""
    if not article_ids:
        return {}
    rows = query(f"SELECT id, story_id, no_news FROM articles WHERE id IN ({_marks(article_ids)})",
                 tuple(article_ids))
    return {r["id"]: "no_news" if r["no_news"] else "merged"
            for r in rows if r["no_news"] or r["story_id"]}


def _attach_related(cards):
    """Each lead's other reports, oldest first, as card['related']."""
    by_id = {c["id"]: c for c in cards}
    for c in cards:
        c["related"] = []
    if not by_id:
        return cards
    ids = list(by_id)
    for r in query(
            "SELECT a.id, a.story_id, a.original_headline, a.straight_headline, a.canonical_url, "
            "a.pub_date, f.name AS source_name FROM articles a "
            "JOIN feed_sources f ON f.id = a.feed_source_id "
            f"WHERE a.story_id IN ({_marks(ids)}) AND a.no_news = 0 AND f.enabled = 1 "
            "ORDER BY a.pub_date", tuple(ids)):
        by_id[r.pop("story_id")]["related"].append(r)
    return cards


def articles_needing_extraction(limit=25):
    """Same priority as get_queue (newest day first, sources taking turns within
    a day) so the per-pass extraction budget goes to the stories offered next,
    and dated stories are never extracted at all."""
    return query(
        "SELECT * FROM ("
        "  SELECT *, substr(pub_date, 1, 10) AS day, ROW_NUMBER() OVER "
        "    (PARTITION BY feed_source_id, substr(pub_date, 1, 10) "
        "     ORDER BY pub_date DESC) AS source_rank "
        "  FROM articles WHERE extracted_at IS NULL AND is_read = 0 AND no_news = 0 "
        "  AND pub_date >= ?"
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
    rows = query(
        "SELECT COUNT(*) AS n FROM articles WHERE ai_provider = 'anthropic' "
        "AND ai_processed_at >= ?", (utc_day(),))
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


# a card of its own: the lead of its story, with news in it
_OFFERED = "a.story_id IS NULL AND a.no_news = 0"


def get_backlog(limit=100):
    """Unread stories older than the max-age window. Never offered in the main
    queue — the end-of-stack card reports the count and reveals them on demand."""
    cutoff = _story_age_cutoff()
    # enabled feeds only, like the queue: a removed feed's stories stay gone
    count = query(
        "SELECT COUNT(*) AS n FROM articles a "
        "JOIN feed_sources f ON f.id = a.feed_source_id "
        f"WHERE a.is_read = 0 AND f.enabled = 1 AND {_OFFERED} AND a.pub_date < ?",
        (cutoff,))[0]["n"]
    items = []
    if limit:
        items = _attach_related([_decode_card(r) for r in query(
            "SELECT a.*, f.name AS source_name FROM articles a "
            "JOIN feed_sources f ON f.id = a.feed_source_id "
            f"WHERE a.is_read = 0 AND f.enabled = 1 AND {_OFFERED} AND a.pub_date < ? "
            "ORDER BY a.pub_date DESC LIMIT ?", (cutoff, limit))])
    return {"count": count, "items": items}


def get_queue(limit=50):
    """Unread cards, newest day first (spec §4.3, adapted). Within a day the
    sources take turns — each source's newest story, then each one's
    second-newest, and so on — so a prolific feed can't crowd the others out
    while yesterday's news never outranks today's. Stories older than the
    max_story_age_days setting, or from disabled/removed feeds, are not offered.
    One card per story: its lead, with the other reports as card['related']."""
    rows = query(
        "SELECT * FROM ("
        "  SELECT a.*, f.name AS source_name, "
        "         substr(a.pub_date, 1, 10) AS day, ROW_NUMBER() OVER "
        "    (PARTITION BY a.feed_source_id, substr(a.pub_date, 1, 10) "
        "     ORDER BY a.pub_date DESC) AS source_rank "
        "  FROM articles a "
        "  JOIN feed_sources f ON f.id = a.feed_source_id "
        f"  WHERE a.is_read = 0 AND f.enabled = 1 AND {_OFFERED} AND a.pub_date >= ?"
        ") ORDER BY day DESC, source_rank, pub_date DESC LIMIT ?",
        (_story_age_cutoff(), limit))
    for r in rows:
        _decode_card(r)
        r.pop("source_rank", None)
        r.pop("day", None)
    return _attach_related(rows)


def get_ticker(limit, max_age_hours):
    """Newest stories for the ticker tape, read or not (it's a tape, not a
    queue), from enabled feeds and no older than the ticker's max age. One
    line per story."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    return query(
        "SELECT a.id, a.canonical_url, a.pub_date, a.ticker_headline, "
        "       a.straight_headline, a.original_headline, a.rss_description, "
        "       f.name AS source_name "
        "FROM articles a JOIN feed_sources f ON f.id = a.feed_source_id "
        f"WHERE f.enabled = 1 AND {_OFFERED} AND a.pub_date >= ? "
        "ORDER BY a.pub_date DESC LIMIT ?", (cutoff, limit))


def save_ticker_headline(article_id, text):
    execute("UPDATE articles SET ticker_headline = ? WHERE id = ?", (text, article_id))


def get_card(article_id):
    """One story in card shape (as get_queue returns them), read or not."""
    rows = query(
        "SELECT a.*, f.name AS source_name FROM articles a "
        "LEFT JOIN feed_sources f ON f.id = a.feed_source_id WHERE a.id = ?",
        (article_id,))
    return _attach_related([_decode_card(rows[0])])[0] if rows else None


def get_article(article_id):
    rows = query("SELECT * FROM articles WHERE id = ?", (article_id,))
    return rows[0] if rows else None


def mark_read(article_id):
    """Reading a story's card reads every report of it."""
    lead = "(SELECT COALESCE(story_id, id) FROM articles WHERE id = ?)"
    execute(f"UPDATE articles SET is_read = 1 WHERE id = {lead} OR story_id = {lead}",
            (article_id, article_id))


def status():
    q = query
    return {
        "unread": q(f"SELECT COUNT(*) AS n FROM articles a WHERE is_read = 0 AND {_OFFERED}")[0]["n"],
        "processed_unread": q(
            "SELECT COUNT(*) AS n FROM articles a "
            f"WHERE is_read = 0 AND processed = 1 AND {_OFFERED}")[0]["n"],
        "grouped": q("SELECT COUNT(*) AS n FROM articles "
                     "WHERE story_id IS NOT NULL AND no_news = 0")[0]["n"],
        "no_news": q("SELECT COUNT(*) AS n FROM articles WHERE no_news = 1")[0]["n"],
        "total": q("SELECT COUNT(*) AS n FROM articles")[0]["n"],
        "by_provider": {r["ai_provider"] or "none": r["n"] for r in q(
            "SELECT ai_provider, COUNT(*) AS n FROM articles WHERE processed = 1 "
            "GROUP BY ai_provider")},
    }
