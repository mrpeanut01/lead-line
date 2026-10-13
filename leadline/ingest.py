"""Ingestion + extraction layers (spec §5).

Feed polling with conditional GET, URL normalization, SHA-256 dedup, then a
dependency-free readability pass (paragraph harvesting) standing in for
Newspaper4k. robots.txt is respected (spec §12).
"""
import hashlib
import re
import time
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

_robots_cache = {}   # host -> (parser, time to ask again, or None for a real answer)
ROBOTS_RETRY_SECONDS = 600


def canonicalize_url(url):
    """Strip tracking parameters and fragments (spec §5.1)."""
    p = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k)]
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(params), ""))


def dedup_hash(title, canonical_url):
    """SHA-256 of normalized_title|canonical_url (spec §4.1, minus its pubDate
    component: feeds that omit, edit, or future-date timestamps — which the
    clamp in _entry_pub_date rewrites to poll time — would make the same story
    hash differently on every poll and resurrect as a 'new' unread card)."""
    normalized_title = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha256(f"{normalized_title}|{canonical_url}".encode()).hexdigest()


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
    parsed = feedparser.parse(resp.content)
    if parsed.entries or not parsed.bozo:   # not an error page served as 200: that is asked for again
        store.update_feed_fetch_meta(
            feed["id"], resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
    new = 0
    for entry in parsed.entries[:50]:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        canonical = canonicalize_url(link)
        pub = _entry_pub_date(entry)
        desc = re.sub(r"<[^>]+>", " ", entry.get("summary", "") or "").strip()
        if store.insert_article(feed["id"], canonical, dedup_hash(title, canonical),
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
        return sum(pool.map(_poll_feed_safely, feeds))


def _poll_feed_safely(feed):
    """One feed's malformed entry must not cost the others their poll."""
    try:
        return poll_feed(feed)
    except Exception as e:
        print(f"[ingest] {feed['name']}: {e}")
        return 0


# --- extraction ---

class _ArticleHTMLParser(HTMLParser):
    """Minimal readability: harvest <p> text outside page chrome, grab og:image."""

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
    """True or False from the site's robots.txt; None while it can't be read
    (5xx, timeout, connection error): not fetched, and asked again later."""
    host = urlparse(url).scheme + "://" + urlparse(url).netloc
    rp, retry_at = _robots_cache.get(host, (None, None))
    if rp is None or (retry_at and time.time() > retry_at):
        retry_at = None
        # fetched here rather than with rp.read(), which has no timeout: one
        # unresponsive host would hang the pipeline and every summary behind it
        rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
        try:
            resp = requests.get(host + "/robots.txt", headers={"User-Agent": USER_AGENT},
                                timeout=10)
            if resp.status_code >= 500:   # an outage, not an answer: ask again later
                rp.disallow_all = True
                retry_at = time.time() + ROBOTS_RETRY_SECONDS
            elif resp.status_code in (401, 403):
                rp.disallow_all = True
            elif resp.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(resp.text.splitlines())
        except Exception:
            rp.disallow_all = True
            retry_at = time.time() + ROBOTS_RETRY_SECONDS
        _robots_cache[host] = (rp, retry_at)
    if retry_at:
        return None
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


FETCH_ATTEMPTS = 3     # then a site that stays down is treated like a paywall
_fetch_failures = {}   # article id -> fetches that found the site down or busy


def fetch_pending(article_id):
    """True while the story's site was down or busy and tries remain: its
    text is still worth waiting for."""
    return article_id in _fetch_failures


def _html_text(resp):
    """The page as text. With no charset in the header requests assumes
    ISO-8859-1, which garbles the UTF-8 most pages are actually in."""
    if "charset" in resp.headers.get("Content-Type", "").lower():
        return resp.text
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        resp.encoding = resp.apparent_encoding
        return resp.text


def extract_article(article):
    """Fetch + extract body; detect paywall by word count (spec §5.2). A site
    that is down or busy is not a paywall: nothing is saved, so the story is
    fetched again later (a few times, so a dead site can't hold up the rest)."""
    body, image = "", None
    allowed = _robots_allowed(article["canonical_url"])
    resp, down = None, allowed is None
    if allowed:
        try:
            resp = requests.get(article["canonical_url"],
                                headers={"User-Agent": USER_AGENT}, timeout=20)
            down = resp.status_code == 429 or resp.status_code >= 500
        except requests.RequestException:
            down = True
    if down:
        _fetch_failures[article["id"]] = _fetch_failures.get(article["id"], 0) + 1
        if _fetch_failures[article["id"]] < FETCH_ATTEMPTS:
            return
    try:
        if resp is not None and resp.ok:
            parser = _ArticleHTMLParser()
            parser.feed(_html_text(resp))
            body = "\n\n".join(parser.paragraphs)
            image = parser.og_image
    except Exception:
        pass

    paywalled = len(body.split()) < config.PAYWALL_WORD_THRESHOLD
    if paywalled:
        # Summarize from the RSS description only (spec §5.2)
        body = article.get("rss_description") or ""
    _fetch_failures.pop(article["id"], None)
    store.save_extraction(article["id"], body, paywalled, image)


def extract_pending():
    articles = store.articles_needing_extraction()
    for a in articles:
        extract_article(a)
    return len(articles)
