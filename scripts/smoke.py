"""Headless smoke test for CI: the store, settings, extraction parser, summary
validation and the summary queue, with no window, network or AI server.

    LEADLINE_DATA_DIR=$(mktemp -d) .venv/bin/python scripts/smoke.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LEADLINE_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent.parent))

from leadline import ai, api, config, ingest, store  # noqa: E402

# settings: defaults, save, clamp, cache picks up the save
assert config.setting("read_ahead") == 1
assert config.save_settings({"read_ahead": 99, "ollama_role": "secondary",
                             "anthropic_role": "secondary"})["read_ahead"] == 10
assert config.setting("read_ahead") == 10
assert ai.enabled_backends() == ["ollama", "anthropic"]   # ties: local server first

# store: feeds, dedup on canonical URL, queue and backlog honor enabled feeds
store.seed_default_feeds()
store.add_feed("Smoke", "http://smoke.test/rss")
feed = next(f for f in store.get_feeds() if f["name"] == "Smoke")
url = ingest.canonicalize_url("http://Smoke.test/a?utm_source=x&id=1#frag")
assert url == "http://smoke.test/a?id=1", url
assert store.insert_article(feed["id"], url, ingest.dedup_hash("T", url), "T", "desc", store.now())
assert not store.insert_article(feed["id"], url, "other-hash", "T2", "desc", store.now())
old = "2020-01-01T00:00:00+00:00"
store.insert_article(feed["id"], "http://smoke.test/old", "h-old", "Old", "", old)
queue = store.get_queue(10)
assert [a["original_headline"] for a in queue] == ["T"], queue
assert store.get_backlog(10)["count"] == 1
store.set_feed_enabled(feed["id"], False)
assert store.get_queue(10) == [] and store.get_backlog(10)["count"] == 0
store.set_feed_enabled(feed["id"], True)

# extraction: paragraphs outside page chrome, og:image, UTF-8 without a charset
parser = ingest._ArticleHTMLParser()
parser.feed('<meta property="og:image" content="http://i/x.jpg"><nav><p>' + "menu " * 10
            + "</p></nav><p>" + "body text " * 10 + "</p>")
assert parser.og_image == "http://i/x.jpg" and len(parser.paragraphs) == 1


class Page:
    headers = {"Content-Type": "text/html"}
    content = "<p>café</p>".encode()


assert ingest._html_text(Page()) == "<p>café</p>"

# a site that is down: asked again (not saved) until the attempts run out, then
# the feed's description without the paywall tag
import requests  # noqa: E402


def unreachable(url, **kwargs):
    raise requests.ConnectionError()


ingest.requests.get, real_get = unreachable, ingest.requests.get
ingest.FETCH_RETRY_SECONDS = ingest.ROBOTS_RETRY_SECONDS = 0
for attempt in range(ingest.FETCH_ATTEMPTS - 1):
    try:
        ingest.ensure_body(queue[0]["id"])
        raise SystemExit("a down site was saved on attempt %d" % (attempt + 1))
    except ingest.SiteDown:
        assert store.get_article(queue[0]["id"])["body_text"] is None
row = ingest.ensure_body(queue[0]["id"])
assert (row["body_text"], row["is_paywalled"]) == ("desc", 0), dict(row)
ingest.requests.get = real_get

# summary validation
good = ('{"straight_headline": "H", "bluf_bullets": ["a", "b", "c"], "one_sentence": "s",'
        ' "topic_tags": ["t"], "confidence": 2}')
assert ai._parse_summary("noise " + good)["confidence"] == 1.0
try:
    ai._parse_summary('{"straight_headline": "H", "bluf_bullets": ["a"], "one_sentence": "s"}')
    raise SystemExit("short bullet list accepted")
except ValueError:
    pass

# think ladder, against a stand-in Ollama server: a model that answers empty at
# "think": false settles on "low" after two calls; one that rejects the field
# (HTTP 400) settles on leaving it out once that is accepted
import json  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

asked = []


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        think = body.get("think", "unset")
        asked.append(think)
        if body["model"] == "rejects-think" and think != "unset":
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        empty = body["model"] == "empty-at-false" and think is False
        reply = json.dumps({"message": {"content": "" if empty else good}, "done": True}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
threading.Thread(target=server.serve_forever, daemon=True).start()
ollama = "http://127.0.0.1:%d" % server.server_port
for model, calls in (("empty-at-false", [[False, "low"], [False, "low"], ["low"]]),
                     ("rejects-think", [[False, "low", "unset"], ["unset"]])):
    for expected in calls:
        asked.clear()
        assert ai._parse_summary(ai._ollama_generate(ollama, model, "prompt"))
        assert asked == expected, (model, asked, expected)
server.shutdown()

# no news: the model's flag (even as a string), or a headline that says so
assert ai._parse_summary('{"no_news": "true", "bluf_bullets": []}') == {"no_news": True}
assert ai._parse_summary(good.replace('"confidence": 2', '"confidence": 2, "no_news": "false"'))[
    "no_news"] is False
meta = good.replace('"H"', '"No substantive news content provided in the article"')
assert ai._parse_summary(meta.replace('"confidence": 2', '"confidence": 0.1')) == {"no_news": True}
assert not ai._parse_summary(meta)["no_news"]   # a confident summary is taken at its word

# stories: a site's review and the galleries posted with it are one card, led by
# the review; another outlet's report of the same news joins it; a daily
# series with the same title is not one story; reading the card reads them all
from datetime import datetime, timedelta, timezone  # noqa: E402

from leadline import stories  # noqa: E402

store.add_feed("Cars", "http://cars.test/rss")
store.add_feed("Wire", "http://wire.test/rss")
cars = next(f["id"] for f in store.get_feeds() if f["name"] == "Cars")
wire = next(f["id"] for f in store.get_feeds() if f["name"] == "Wire")
t0 = datetime.now(timezone.utc) - timedelta(hours=30)


def entry(feed_id, title, hours, desc=""):
    url = f"http://x.test/{hours}/" + title.lower().replace(" ", "-")
    return {"feed_source_id": feed_id, "canonical_url": url,
            "dedup_hash": ingest.dedup_hash(title, url), "original_headline": title,
            "rss_description": desc, "pub_date": (t0 + timedelta(hours=hours)).isoformat()}


index = stories.Index.recent()[0]
assert stories.add_articles(index, [
    entry(cars, "View Interior Photos of the 2027 Honda Passport TrailSport", 0, "See it"),
    entry(cars, "View Exterior Photos of the 2027 Honda Passport TrailSport", 0, "See it"),
    entry(cars, "Tested: 2027 Honda Passport TrailSport Is Legit Off-Road", 0, "A long review"),
    entry(cars, "2027 Lexus NX Gets a Hybrid-Only Lineup", 1),
    entry(wire, "Morning news brief", 0),
    entry(wire, "Pope Leo says he will gradually resume duties after cancer surgery", 2),
]) == 6
assert stories.add_articles(index, [
    entry(wire, "Morning news brief", 24),
    entry(feed["id"], "Pope Leo tells faithful he will gradually resume duties after cancer surgery", 5),
]) == 2
cards = {c["original_headline"]: c for c in store.get_queue(100)}
honda = cards["Tested: 2027 Honda Passport TrailSport Is Legit Off-Road"]
assert len(honda["related"]) == 2, honda["related"]
assert not any("View" in h for h in cards), list(cards)
assert "2027 Lexus NX Gets a Hybrid-Only Lineup" in cards
assert [c["original_headline"] for c in store.get_queue(100)].count("Morning news brief") == 2
pope = cards["Pope Leo says he will gradually resume duties after cancer surgery"]
assert [r["source_name"] for r in pope["related"]] == ["Smoke"], pope["related"]
store.mark_read(honda["id"])
assert all(store.is_read(r["id"]) for r in honda["related"])
late = entry(cars, "View More Photos of the 2027 Honda Passport TrailSport", 3)
assert stories.add_articles(index, [late]) == 1   # a near copy of a read story: arrives read
late_id = store.query("SELECT id FROM articles WHERE canonical_url = ?", (late["canonical_url"],))[0]["id"]
assert store.is_read(late_id) and store.current_lead(late_id)["id"] == honda["id"]
assert stories.add_articles(index, [late]) == 0   # already there

# a later development of a story already read is news: a card of its own
first = entry(wire, "Zelensky to meet Putin in Istanbul next week for ceasefire talks", 6)
stories.add_articles(index, [first])
first_id = next(c["id"] for c in store.get_queue(100) if c["original_headline"] == first["original_headline"])
store.mark_read(first_id)
later = entry(feed["id"], "Zelensky meeting with Putin in Istanbul collapses as Russia walks out", 9)
sim = index.similarity(index.doc(later, article_id="x"), index.docs[first_id])
assert stories.MATCH <= sim < stories.COPY, sim   # would have grouped before
stories.add_articles(index, [later])
assert later["original_headline"] in [c["original_headline"] for c in store.get_queue(100)]

# joins are checked when made: no story joins itself or a story's member
assert store.join_story(honda["id"], [honda["id"]]) == 0
assert store.join_story(honda["id"], [late_id]) == 0
assert store.join_story(late_id, [first_id]) == 0
assert store.get_article(honda["id"])["story_id"] is None

# a lead with no news hides; the best of its story's other reports leads instead
stories.drop_no_news(pope["id"], "ollama", "smoke")
cards = {c["original_headline"]: c for c in store.get_queue(100)}
assert "Pope Leo says he will gradually resume duties after cancer surgery" not in cards
pope2 = cards["Pope Leo tells faithful he will gradually resume duties after cancer surgery"]
assert pope2["related"] == [] and store.status()["no_news"] == 1
bridge = api.Api()
assert bridge.hidden_cards([pope["id"], pope2["id"]]) == {pope["id"]: "no_news"}

# turning off a lead's feed: another feed's report leads the story, still
# offered, and with the summary the story already had (not paid for again)
duel = [entry(wire, "Bose acquires Skullcandy headphone brand in surprise deal", 12),
        entry(cars, "Bose acquires Skullcandy headphone brand in surprise deal today", 12.5)]
stories.add_articles(index, duel)
bose = next(c for c in store.get_queue(100) if c["original_headline"].startswith("Bose"))
assert len(bose["related"]) == 1
store.save_summary(bose["id"], ai._parse_summary(good), "anthropic", "claude-smoke")
claude_calls = store.anthropic_calls_today()
bridge.set_feed_enabled(bose["feed_source_id"], False)
offered = [c for c in store.get_queue(100) if c["original_headline"].startswith("Bose")]
assert [c["id"] for c in offered] == [bose["related"][0]["id"]], offered
assert offered[0]["processed"] and offered[0]["straight_headline"] == "H", offered[0]
assert store.anthropic_calls_today() == claude_calls   # carried over, not a new call
bridge.set_feed_enabled(bose["feed_source_id"], True)

# a feed turned off stays out of a story: not fetched or summarized as one of
# its reports, and never made its lead (it would never be offered)
store.add_feed("Muted", "http://muted.test/rss")
muted = next(f["id"] for f in store.get_feeds() if f["name"] == "Muted")
stories.add_articles(index, [
    entry(wire, "Ferry capsizes off Java with 130 passengers aboard", 13),
    entry(cars, "Ferry capsizes off Java with 130 passengers aboard, rescue under way", 13.2)])
stories.add_articles(index, [   # joins later, as a member: the best-ranked report of all
    entry(muted, "Ferry capsizes off Java with 130 passengers aboard: what we know so far "
                 "about the rescue and the ship", 13.4, "A much longer report. " * 20)])
ferry = next(c for c in store.get_queue(100) if c["original_headline"].startswith("Ferry"))
assert ferry["feed_source_id"] != muted and len(ferry["related"]) == 2, ferry
bridge.set_feed_enabled(muted, False)   # it doesn't lead, so nothing is re-led
assert muted not in [m["feed_source_id"] for m in stories.summary_members(ferry["id"])]
stories.drop_no_news(ferry["id"])
offered = [c for c in store.get_queue(100) if c["original_headline"].startswith("Ferry")]
assert len(offered) == 1 and offered[0]["feed_source_id"] not in (muted, ferry["feed_source_id"]), offered

# a real headline that happens to start like a no-news one is kept
for real in ("No details released on suspect in Austin shooting",
             "No details provided on suspect's motive in Austin shooting",
             "No information yet on Article 5 response, NATO says"):
    reply = good.replace('"H"', f'"{real}"').replace('"confidence": 2', '"confidence": 0.3')
    assert not ai._parse_summary(reply)["no_news"], real
assert bridge.request_summaries([pope["id"]])["states"][pope["id"]] == {
    "state": "hidden", "reason": "no_news"}

# closer calls go to the AI: a shared name ("iphone duo") makes a candidate,
# and the ones it confirms fold into the story being summarized
duo = [entry(feed["id"], t, 10 + i) for i, t in enumerate((
    "iPhone Duo review: The future is foldable",
    "iPhone Duo adding Apple Pencil support next week",
    "Samsung plans six foldable phones for next year"))]
stories.add_articles(index, duo)
duo = [store.get_article(c["id"]) for c in store.get_queue(100)
       if c["original_headline"] in [d["original_headline"] for d in duo]]
review = next(a for a in duo if a["original_headline"].startswith("iPhone Duo review"))
pencil = next(a for a in duo if "Pencil" in a["original_headline"])
assert [c["id"] for c in stories.fold_candidates(review)] == [pencil["id"]]
assert stories.fold_candidates(review, keep=[pencil["id"]]) == []
real_route, real_ollama, real_ready = ai._route, ai.call_ollama, ai.ollama_ready
# Ollama only, like the ticker: with its model not loaded nothing is asked
# (and Claude never is), and the stories stay apart
ai._route = lambda *a: (_ for _ in ()).throw(AssertionError("asked Claude's router"))
ai.call_ollama = lambda *a: (_ for _ in ()).throw(AssertionError("asked a model not loaded"))
ai.ollama_ready = lambda: False
ai.fold_related(review)
assert bridge.hidden_cards([pencil["id"]]) == {}
ai.ollama_ready = lambda: True
ai.call_ollama = lambda prompt, schema, parse: parse('{"same": [1, 7]}')
ai.fold_related(review)
ai._route, ai.call_ollama = real_route, real_ollama
assert bridge.hidden_cards([pencil["id"]]) == {pencil["id"]: "merged"}
assert [r["id"] for r in store.get_card(review["id"])["related"]] == [pencil["id"]]

# JSON left in a bullet is cleaned off; a quote that belongs there stays
assert ai._clean("Runs through 2028.”,") == "Runs through 2028."
assert ai._clean('He said "no."') == 'He said "no."'
assert ai._clean("He said “no.”") == "He said “no.”"

# a summary may not add a year or age the article doesn't state: asked again
# once with a correction, and what is still made up is dropped
article_text = {"original_headline": "QB to miss rest of season", "pub_date": "2026-10-19T19:42:53+00:00",
                "body_text": "Hurts, 28, will miss the rest of the season. His deal runs through 2028."}
prompt = ai.build_prompt(article_text, "Wire")
assert "PUBLISHED: Monday, October 19, 2026" in prompt and "TODAY: " in prompt
# checked against the article's own text: the 19 of its date isn't an age it
# states, but the year it was published is one the model may use
known = ai._known(ai._reports(article_text, "Wire"))
assert ai._invented("The 19-year-old, 28-year-old and the 2026 season", known) == {"19-year-old"}
made_up = ('{"straight_headline": "QB out for season", "bluf_bullets": ["Out for the 2024 season.", '
           '"The 23‑year‑old has clots.", "His deal runs through 2028.", "The 28-year-old will '
           'recover.", "He is on injured reserve."], "one_sentence": "s", "topic_tags": [], "confidence": 0.9}')
honest = made_up.replace("the 2024 season", "the season").replace("23‑year‑old", "quarterback")
replies = []


def fake_route(prompt, schema, parse):
    replies.append(prompt)
    return parse(made_up if len(replies) < 3 else honest), "ollama", "smoke"


ai._route = fake_route
summary, _, _ = ai.summarize(article_text, "Wire")
assert len(replies) == 2 and "said 2024, 23-year-old, which" in replies[1], replies[1][-120:]
assert summary["bluf_bullets"] == ["His deal runs through 2028.", "The 28-year-old will recover.",
                                   "He is on injured reserve."], summary
replies.append("third call answers honestly")
assert ai.summarize(article_text, "Wire")[0]["bluf_bullets"][0] == "Out for the season."

# every paid Claude reply the date check throws away counts against the daily
# cap; one that is trimmed and saved is counted from the database instead
headline_made_up = made_up.replace('"QB out for season"', '"QB out for 2024 season"')


def claude_route(*replies):
    queue = list(replies)

    def route(prompt, schema, parse):
        reply = queue.pop(0)
        if reply is None:
            raise RuntimeError("anthropic: HTTP 529")
        return parse(reply), "anthropic", "claude-smoke"
    return route


def unsaved_delta(*replies):
    ai._anthropic_calls_today()
    before = ai._anthropic_unsaved[1]
    ai._route = claude_route(*replies)
    try:
        ai.summarize(article_text, "Wire")
        outcome = "saved"
    except ValueError:
        outcome = "rejected"
    return outcome, ai._anthropic_unsaved[1] - before


assert unsaved_delta(headline_made_up, headline_made_up) == ("rejected", 2)
assert unsaved_delta(made_up, honest) == ("saved", 1)
assert unsaved_delta(made_up, None) == ("saved", 0)       # retry failed: first one, trimmed
assert unsaved_delta(headline_made_up, None) == ("rejected", 1)
ai._route = real_route

# the upgrade redoes unread summaries made before the date check that state a
# year their article doesn't; a summary true to its article is kept
redo_ids = []
for n, (said, body) in enumerate((("Out for the 2024 season.", "He will miss the season."),
                                  ("Deal runs through 2028.", "His deal runs through 2028."))):
    url = f"http://x.test/redo/{n}"
    article_id = store.insert_article(wire, url, ingest.dedup_hash(said, url), "QB out", "", store.now())
    store.save_extraction(article_id, body, False, None)
    store.save_summary(article_id, dict(ai._parse_summary(good), bluf_bullets=[said, "b", "c"]),
                       "ollama", "smoke")
    redo_ids.append(article_id)
assert ai.redo_invented_summaries() == 1
assert [store.get_article(i)["processed"] for i in redo_ids] == [0, 1]

# the whole path, AI stubbed: the lead's summary first asks which nearby story
# is the same, folds it in, then summarizes both; when that finds no news,
# every report it read is hidden
pod = [entry(wire, "Claude Duet arrives for paying subscribers", 20, "Anthropic launches an agent."),
       entry(cars, "Hands on with Claude Duet calendar tricks", 22, "We tried the agent.")]
stories.add_articles(index, pod)
pod = [store.get_article(c["id"]) for c in store.get_queue(100) if "Claude Duet" in c["original_headline"]]
assert len(pod) == 2, pod   # not alike enough to group without asking
lead, other = sorted(pod, key=lambda a: a["pub_date"])
asked = []


def stub_ollama(prompt, schema, parse):   # the same-story question
    asked.append(prompt)
    return parse('{"same": [1]}')


def stub_route(prompt, schema, parse):      # the summary
    asked.append(prompt)
    return parse('{"no_news": true}'), "ollama", "smoke"


ai._route, ai.call_ollama = stub_route, stub_ollama
real_ensure = ingest.ensure_body
ingest.ensure_body = lambda article_id, for_reader=True: store.get_article(article_id)
ai.summarize_article(lead["id"], keep=[])
ai._route, ai.call_ollama, ingest.ensure_body = real_route, real_ollama, real_ensure
ai.ollama_ready = real_ready
assert "Hands on with Claude Duet" in asked[0] and "REPORT 2" in asked[1], asked
assert bridge.hidden_cards([lead["id"], other["id"]]) == {lead["id"]: "no_news", other["id"]: "no_news"}

# a combined summary sees every report, each with its source
prompt = ai.build_prompt(dict(review, body_text="Review text."), "Smoke",
                         [dict(pencil, source_name="Wire", body_text=None,
                               rss_description="Pencil text.")])
assert "2 reports below" in prompt and "REPORT 2 (Wire, published " in prompt
assert "Pencil text." in prompt

# summary queue: a wanted story is summarized and comes back with its card; a
# story that no longer exists is reported, not queued
article = queue[0]
summary = ai._parse_summary(good)
ingest.ensure_body = lambda article_id, for_reader=True: None
ai.note_demand = lambda: None
ai.summarize_article = lambda article_id, epoch=None, keep=(): store.save_summary(
    article_id, summary, "ollama", "smoke")
bridge = api.Api()
states = bridge.request_summaries([article["id"], "missing"])["states"]
assert states["missing"] == {"state": "gone"}, states
assert states[article["id"]]["state"] in ("queued", "working"), states
for _ in range(50):
    state = bridge.request_summaries([article["id"]])["states"][article["id"]]
    if state["state"] == "done":
        break
    time.sleep(0.1)
assert state["state"] == "done" and state["card"]["straight_headline"] == "H", state
assert bridge.request_summaries([])["states"] == {}

# share: plain text, the link alone on the last line (Messages previews a
# trailing link); stray newlines folded; the clipboard itself is stubbed
from leadline import share  # noqa: E402

copied = []
share.copy_to_clipboard = lambda text: copied.append(text) or True
res = bridge.share_story(article["id"])
assert res["ok"] and copied == [res["text"]], res
assert res["text"] == ("H\n\n• a\n• b\n• c\n\n"
                       "Original headline (Smoke): “T”\nhttp://smoke.test/a?id=1"), res["text"]
assert share.share_text({"processed": 0, "straight_headline": None, "original_headline": "Old\nnews",
                         "source_name": "", "canonical_url": "http://x/1"}) == (
    "Original headline: “Old news”\nhttp://x/1")
assert bridge.share_story("missing")["ok"] is False

print("smoke ok")
