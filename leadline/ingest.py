"""Ingestion + extraction layers (spec §5).

Feed polling with conditional GET, URL normalization, SHA-256 dedup, then a
dependency-free readability pass (paragraph harvesting) standing in for
Newspaper4k. robots.txt is respected (spec §12).
"""
import hashlib
import re
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests

from . import config, store

USER_AGENT = "LeadLine/1.0 (personal news reader)"
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|cmpid|ito$)", re.I)

_robots_cache = {}


def canonicalize_url(url):
    """Strip tracking parameters and fragments (spec §5.1)."""
    p = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k)]
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(params), ""))


def dedup_hash(title, pub_date, canonical_url):
    """SHA-256 of normalized_title|pubDate_utc_minute|canonical_url (spec §4.1)."""
    minute = (pub_date or "")[:16]  # ISO timestamp truncated to minute
    normalized_title = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha256(f"{normalized_title}|{minute}|{canonical_url}".encode()).hexdigest()


def _entry_pub_date(entry):
    now = datetime.now(timezone.utc)
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            # Clamp future-dated entries (misconfigured feeds) so they can't
            # pin themselves to the top of the newest-day-first queue.
            return min(datetime(*t[:6], tzinfo=timezone.utc), now).isoformat()
    return now.isoformat()


def poll_feed(feed):
    """Conditional GET; on 304 stop, on 200 store validators then parse (spec §5.1)."""
    headers = {"User-Agent": USER_AGENT}
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]
    try:
        resp = requests.get(feed["rss_url"], headers=headers, timeout=20)
    except requests.RequestException:
        return 0
    if resp.status_code == 304:
        store.update_feed_fetch_meta(feed["id"], feed["etag"], feed["last_modified"])
        return 0
    if resp.status_code != 200:
        return 0
    store.update_feed_fetch_meta(
        feed["id"], resp.headers.get("ETag"), resp.headers.get("Last-Modified"))

    parsed = feedparser.parse(resp.content)
    new = 0
    for entry in parsed.entries[:50]:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        canonical = canonicalize_url(link)
        pub = _entry_pub_date(entry)
        desc = re.sub(r"<[^>]+>", " ", entry.get("summary", "") or "").strip()
        if store.insert_article(feed["id"], canonical, dedup_hash(title, pub, canonical),
                                title.strip(), desc, pub):
            new += 1
    return new


def poll_all_feeds():
    """Poll every enabled feed concurrently so a slow or unreachable source
    (each has a 20 s timeout) can't delay or starve the others."""
    feeds = store.get_feeds(enabled_only=True)
    if not feeds:
        return 0
    with ThreadPoolExecutor(max_workers=min(8, len(feeds))) as pool:
        return sum(pool.map(poll_feed, feeds))


# --- extraction ---

class _ArticleHTMLParser(HTMLParser):
    """Minimal readability: harvest <p> text, prefer <article> scope, grab og:image."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs = []
        self.og_image = None
        self._in_p = False
        self._skip_depth = 0
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("property") == "og:image" and a.get("content"):
            self.og_image = self.og_image or a["content"]
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "p" and self._skip_depth == 0:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) > 30:
                self.paragraphs.append(text)

    def handle_data(self, data):
        if self._in_p:
            self._buf.append(data)


def _robots_allowed(url):
    host = urlparse(url).scheme + "://" + urlparse(url).netloc
    rp = _robots_cache.get(host)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp.allow_all = True
        _robots_cache[host] = rp
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def extract_article(article):
    """Fetch + extract body; detect paywall by word count (spec §5.2)."""
    body, image = "", None
    if _robots_allowed(article["canonical_url"]):
        try:
            resp = requests.get(article["canonical_url"],
                                headers={"User-Agent": USER_AGENT}, timeout=20)
            if resp.ok:
                parser = _ArticleHTMLParser()
                parser.feed(resp.text)
                body = "\n\n".join(parser.paragraphs)
                image = parser.og_image
        except (requests.RequestException, Exception):
            pass

    paywalled = len(body.split()) < config.PAYWALL_WORD_THRESHOLD
    if paywalled:
        # Summarize from the RSS description only (spec §5.2)
        body = article.get("rss_description") or ""
    store.save_extraction(article["id"], body, paywalled, image)


def extract_pending():
    articles = store.articles_needing_extraction()
    for a in articles:
        extract_article(a)
    return len(articles)
