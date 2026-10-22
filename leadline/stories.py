"""Story grouping: one card per story, however many articles report it.

A story is a lead article (story_id NULL) plus its members (story_id = the
lead's id). The lead carries the card and the combined summary; reading it
reads the whole story. Once a story has been read or summarized, only a near
copy of it (or a page its site posted with it) still joins it, arriving read
if the story was: anything less alike may be a new development, and gets a
card of its own.

New articles are grouped as they are polled, by how much their headline shares
with the last couple of days of headlines. Words are weighted by rarity, so a
model name or a place counts for far more than "Apple" or "Trump". That catches
the same news from several outlets and the photo galleries, spec pages and
review a site posts for one car — with no model involved. Closer calls (the same
product from a different angle) are put to the AI just before the story is
summarized: see fold_candidates() and ai.summarize_article().
"""
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import store

MATCH = 0.5             # headline similarity that makes two articles one story
SAME_BATCH = 0.4        # ...for one site's articles posted within SAME_BATCH_GAP
COPY = 0.8              # ...for joining a story already read or summarized: a near copy
                        # ("Pope hospitalized" vs "Pope dies" is 0.58, and news)
SAME_BATCH_GAP = timedelta(minutes=15)
MATCH_WINDOW = timedelta(hours=36)    # between two articles of one story
STORY_WINDOW = timedelta(hours=48)    # between a story's lead and its newest report
RECURRING_GAP = timedelta(hours=12)   # one site's identical headline this far apart is a series
FOLD_FLOOR = 0.25       # closest headlines short of MATCH, put to the AI
FOLD_MAX = 6            # most of those asked about at once
CORPUS_DAYS = 7         # headlines that set how rare a word is

_STOP = frozenset("""
a an the and or but nor of in on at to for from by with without as is are was were be been
being it its this that these those there their they them he she his her we our you your i me
my us do does did done has have had having not no yes so than then too very can could will
would should may might must new news says say said saying report reports reported reportedly
how why what when where who whom which while here heres now just more most less first last
after before over under about into out up down off again all any some each every other another
one two three get gets got make makes made take takes vs via per amid against between through
during since until view photos photo pictures interior exterior gallery video videos watch
tested driven review reviews hands podcast episode deals deal live update updates explained
analysis opinion newsletter recap
""".split())
_MODEL_CODE = re.compile(r"^([a-z]{1,4})(\d{2,4})[a-z]?$")   # rx350, gle63 -> also rx, gle
# a gallery or video page that goes with a story rather than telling it
_FILLER = re.compile(r"^\s*(?:view|see|watch)\b[^:]{0,40}\b(?:photos?|pictures|gallery|images|video)\b"
                     r"|^\s*(?:photos?|gallery|video)\s*:", re.I)

# One change to stories at a time: a poll's new articles, the upgrade's
# backfill, a fold, a new lead. A new article then never joins a story through
# a lead that is being replaced.
_lock = threading.Lock()


def _stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _words(headline):
    """The headline's telling words in order: lowercased, stemmed, no stopwords
    or gallery/review boilerplate."""
    text = unicodedata.normalize("NFKD", headline or "").lower()
    text = text.replace("’", "'").replace("'s ", " ")
    return [_stem(w) for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text)
            if len(w) > 1 and w not in _STOP]


def tokens(headline):
    """The headline's telling words as a set. A model code (rx350) also counts
    as its letters (rx), so it matches the model line."""
    out = set()
    for w in _words(headline):
        m = _MODEL_CODE.match(w)
        if m and m.group(1) not in _STOP:
            out.add(m.group(1))
        out.add(w)
    return out


def _pairs(headline):
    """Adjacent telling words: a name like "iphone duo" or "tesla roadster"."""
    words = _words(headline)
    return {(a, b) for a, b in zip(words, words[1:]) if a != b}


def rank(article):
    """How well an article can lead its story: a report beats a gallery page,
    and text beats a one-line description."""
    body = article.get("body_len")
    if body is None:
        body = len(article.get("body_text") or "")
    return (not _FILLER.search(article.get("original_headline") or ""),
            bool(article.get("processed")),
            body // 500,
            len(article.get("rss_description") or ""))


def _when(iso):
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class _Doc:
    __slots__ = ("id", "feed", "when", "lead", "key", "weights", "norm")


class Index:
    """Recent headlines, weighted by how rare each word is among them."""

    def __init__(self, rows):
        self.docs = {}
        self._postings = defaultdict(set)
        self._df = Counter()
        for r in rows:
            self._df.update(tokens(r["original_headline"]))
        self._n = len(rows)

    @classmethod
    def recent(cls, add=True):
        """Index of the last CORPUS_DAYS of headlines (stories with no news left
        out: nothing should join one)."""
        since = (datetime.now(timezone.utc) - timedelta(days=CORPUS_DAYS)).isoformat()
        rows = store.grouping_rows(since)
        index = cls(rows)
        if add:
            for r in rows:
                index.add(r)
        return index, rows

    def doc(self, article, article_id=None, lead=None):
        d = _Doc()
        d.id = article_id or article["id"]
        d.feed = article["feed_source_id"]
        d.when = _when(article["pub_date"])
        d.lead = lead or article.get("story_id") or d.id
        d.key = " ".join((article["original_headline"] or "").lower().split())
        n1 = self._n + 1
        d.weights = {t: math.log(n1 / (self._df.get(t, 0) + 1)) + 1
                     for t in tokens(article["original_headline"])}
        d.norm = math.sqrt(sum(w * w for w in d.weights.values())) or 1.0
        return d

    def add(self, article, article_id=None, lead=None):
        d = self.doc(article, article_id, lead)
        self.docs[d.id] = d
        for t in d.weights:
            self._postings[t].add(d.id)
        return d

    def similarity(self, a, b):
        dot = sum(w * b.weights[t] for t, w in a.weights.items() if t in b.weights)
        return dot / (a.norm * b.norm)

    def best_match(self, d):
        """(lead id, similarity, same site at the same time) of the story d
        belongs to, or None."""
        dots = Counter()
        for t, w in d.weights.items():
            for other in self._postings.get(t, ()):
                if other != d.id:
                    dots[other] += w * self.docs[other].weights[t]
        best = None
        for other_id, dot in dots.items():
            o = self.docs[other_id]
            gap = abs(d.when - o.when)
            lead = self.docs.get(o.lead)
            if gap > MATCH_WINDOW or not lead or abs(d.when - lead.when) > STORY_WINDOW:
                continue
            same_feed = o.feed == d.feed
            if same_feed and o.key == d.key and gap > RECURRING_GAP:
                continue   # "Morning news brief" is a new one each day
            sim = dot / (d.norm * o.norm)
            batch = same_feed and gap <= SAME_BATCH_GAP
            if sim >= (SAME_BATCH if batch else MATCH) and (best is None or sim > best[1]):
                best = (o.lead, sim, batch)
        return best


def _joinable(match, lead):
    """Whether a match may join the story led by `lead` (a row with is_read,
    processed): always while nobody has read or summarized it; after that only
    a near copy or a page posted along with it."""
    return not (lead["is_read"] or lead["processed"]) or match[1] >= COPY or match[2]


def add_articles(index, entries):
    """Insert a poll's entries, each into the story it belongs to (or a story of
    its own). The best-ranked go first, so a site's review leads the galleries
    posted with it. Returns how many were new."""
    new = 0
    with _lock:
        fresh = [e for e in entries
                 if not store.article_exists(e["canonical_url"], e["dedup_hash"])]
        for e in sorted(fresh, key=rank, reverse=True):
            d = index.doc(e, article_id="?")
            match = index.best_match(d)
            # the index is as of the poll's start: since then the summary
            # worker may have folded that story into another, or found it had
            # no news
            lead = match and store.current_lead(match[0])
            if lead and not _joinable(match, lead):
                lead = None
            article_id = store.insert_article(
                e["feed_source_id"], e["canonical_url"], e["dedup_hash"],
                e["original_headline"], e["rss_description"], e["pub_date"],
                story_id=lead and lead["id"], is_read=bool(lead and lead["is_read"]))
            if article_id:
                index.add(e, article_id, lead and lead["id"])
                new += 1
    return new


def backfill():
    """Group the stories already in the database: once, after an upgrade from a
    version without grouping. Read reports go first, so a near copy of one
    arrives read; any other report of a read story stays a story of its own."""
    with _lock:
        index, rows = Index.recent(add=False)
        rows = [r for r in rows if not r["story_id"]]
        rows.sort(key=lambda r: (r["is_read"], rank(r), r["pub_date"]), reverse=True)
        by_id = {r["id"]: r for r in rows}
        joined = defaultdict(list)
        for r in rows:
            d = index.doc(r)
            match = index.best_match(d)
            if match and _joinable(match, by_id[match[0]]):
                d.lead = match[0]
                joined[match[0]].append(r["id"])
            index.docs[d.id] = d
            for t in d.weights:
                index._postings[t].add(d.id)
        for lead, ids in joined.items():
            store.join_story(lead, ids)
        return sum(len(ids) for ids in joined.values())


def enabled_members(lead_id):
    """The story's other reports from feeds that are on: a feed the reader
    turned off is never fetched, summarized, or put on a card."""
    return [m for m in store.story_members(lead_id) if m["feed_enabled"]]


def summary_members(lead_id, limit=3):
    """The story's other reports to combine into its summary, best first."""
    return sorted(enabled_members(lead_id), key=rank, reverse=True)[:limit]


def fold_candidates(lead, keep=()):
    """Unread, unsummarized stories near the lead in time that come close to
    its story without being a sure match, for the AI to judge: headlines that
    are fairly alike, share a name ("iphone duo"), or were posted by the same
    site at the same time. `keep`: stories not to fold (the one on screen)."""
    when = _when(lead["pub_date"])
    others = [o for o in store.unread_story_leads(
        (when - MATCH_WINDOW).isoformat(), (when + MATCH_WINDOW).isoformat(), exclude=lead["id"])
        if o["id"] not in keep]
    if not others:
        return []
    index, _ = Index.recent(add=False)
    group = [lead, *store.story_members(lead["id"])]
    docs = [index.doc(a) for a in group]
    pairs = set().union(*(_pairs(a["original_headline"]) for a in group))
    scored = []
    for other in others:
        d = index.doc(other)
        sim = max(index.similarity(d, g) for g in docs)
        same_batch = any(d.feed == g.feed and abs(d.when - g.when) <= SAME_BATCH_GAP
                         for g in docs)
        if sim >= FOLD_FLOOR or (sim > 0 and same_batch) or pairs & _pairs(other["original_headline"]):
            scored.append((sim, other))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [other for _, other in scored[:FOLD_MAX]]


def drop_no_news(article_id, provider=None, model=None, also=()):
    """The article turned out to have no news: hide it, and `also` (the story's
    other reports the AI read with it). If the story has others from feeds
    that are on, the best of them leads it now (one from a feed that is off
    would never be offered). provider/model: the AI that just found that out
    (None: an old summary, already counted)."""
    with _lock:
        store.mark_no_news(article_id, provider, model)
        for other in also:
            store.mark_no_news(other)
        members = store.story_members(article_id)
        enabled = [m for m in members if m["feed_enabled"]]
        if enabled:
            lead = max(enabled, key=rank)
            store.set_lead(lead["id"], [m["id"] for m in members] + [article_id])


def fold(lead_id, article_ids):
    """The AI says these unread stories are the lead's story: make them part
    of it, if nothing changed meanwhile (see store.join_story)."""
    with _lock:
        return store.join_story(lead_id, article_ids, unread_only=True)


def release_feed(feed_id):
    """Before a feed is turned off or removed: its stories that enabled feeds
    also reported are led by one of those reports instead, so they stay on
    offer, with the summary they already have."""
    with _lock:
        for lead_id in store.leads_from_feed(feed_id):
            members = store.story_members(lead_id)
            others = [m for m in members if m["feed_enabled"] and m["feed_source_id"] != feed_id]
            if others:
                lead = max(others, key=rank)
                store.set_lead(lead["id"], [m["id"] for m in members] + [lead_id])
                store.carry_summary(lead_id, lead["id"])
