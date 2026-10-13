"""Ingestion + extraction layers (spec §5).

Feed polling with conditional GET, URL normalization, SHA-256 dedup, then a
dependency-free readability pass (paragraph harvesting) standing in for
Newspaper4k. robots.txt is respected (spec §12).
"""
import hashlib
import re
import threading
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
ROBOTS_RETRY_SECONDS = 60   # before asking for a robots.txt that could not be read again


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


class SiteDown(Exception):
    """The story's site, or its robots.txt, is down or busy. `retry_at` is the
    earliest time worth asking again."""

    def __init__(self, retry_at):
        super().__init__("article site unreachable")
        self.retry_at = retry_at


def _robots_allowed(url):
    """Whether the site's robots.txt lets us fetch url. Raises SiteDown while
    robots.txt can't be read (5xx, timeout, connection error): the page is not
    fetched on a guess."""
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
                retry_at = time.time() + ROBOTS_RETRY_SECONDS
            elif resp.status_code in (401, 403):
                rp.disallow_all = True
            elif resp.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(resp.text.splitlines())
        except Exception:
            retry_at = time.time() + ROBOTS_RETRY_SECONDS
        _robots_cache[host] = (rp, retry_at)
    if retry_at:
        raise SiteDown(retry_at)
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


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


# --- a story's text, and what happens while its site is down ---
#
# ensure_body() is the one way in. A site that is down or busy is not a
# paywall: nothing is saved and SiteDown says when to ask again. After
# FETCH_ATTEMPTS for the reader, the story falls back to the feed's description
# so a dead site can't hold its summary up for good. The background pass only
# pre-fetches: it neither uses up attempts nor falls back.

FETCH_ATTEMPTS = 3
FETCH_RETRY_SECONDS = 20   # before asking a page that was just down or busy again
_fetch_failures = {}   # article id -> (times its site was found down, when to ask again)
_fetch_guard = threading.Lock()
_fetch_locks = {}      # article id -> its fetch lock, kept for the session so every caller shares it


def forget_failures():
    """The reader asked to try again now: sites found down (and robots.txt
    files that couldn't be read) are asked again at once, with fresh tries."""
    _fetch_failures.clear()
    # a snapshot: fetch threads add hosts to the cache while this runs
    for host, (_, retry_at) in list(_robots_cache.items()):
        if retry_at:
            _robots_cache.pop(host, None)


def ensure_body(article_id, for_reader=True):
    """The story's row with its text fetched if it wasn't there (body_text is
    None: never extracted, or TTL-purged; '' means extraction found nothing, so
    it isn't refetched). None if the story is gone. Raises SiteDown while its
    site is down and tries remain (always, when not for_reader). Callers that
    overlap share one fetch."""
    with _fetch_guard:
        lock = _fetch_locks.setdefault(article_id, threading.Lock())
    with lock:
        article = store.get_article(article_id)
        if article and article["body_text"] is None:
            _extract(article, for_reader)
            article = store.get_article(article_id)
        return article


def _extract(article, for_reader):
    """Fetch + extract body; detect paywall by word count (spec §5.2)."""
    failures, retry_at = _fetch_failures.get(article["id"], (0, 0))
    if time.time() < retry_at:
        raise SiteDown(retry_at)
    body, image, resp, down = "", None, None, None
    try:
        if _robots_allowed(article["canonical_url"]):
            resp = requests.get(article["canonical_url"],
                                headers={"User-Agent": USER_AGENT}, timeout=20)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise SiteDown(time.time() + FETCH_RETRY_SECONDS)
    except requests.RequestException:
        down = SiteDown(time.time() + FETCH_RETRY_SECONDS)
    except SiteDown as e:
        down = e
    if down and not for_reader:
        _fetch_failures[article["id"]] = (failures, down.retry_at)
        raise down
    if down and failures + 1 < FETCH_ATTEMPTS:
        _fetch_failures[article["id"]] = (failures + 1, down.retry_at)
        raise down
    try:
        if not down and resp is not None and resp.ok:
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
    # a site that stayed down gets the description too, but isn't a paywall
    store.save_extraction(article["id"], body, paywalled and not down, image)


def extract_pending():
    articles = store.articles_needing_extraction()
    for a in articles:
        try:
            ensure_body(a["id"], for_reader=False)
        except SiteDown:
            pass   # asked again on a later pass, or when the reader gets to it
    return len(articles)
