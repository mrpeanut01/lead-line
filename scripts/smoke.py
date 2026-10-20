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

# summary queue: a wanted story is summarized and comes back with its card; a
# story that no longer exists is reported, not queued
article = queue[0]
summary = ai._parse_summary(good)
ingest.ensure_body = lambda article_id, for_reader=True: None
ai.note_demand = lambda: None
ai.summarize_article = lambda article_id, epoch=None: store.save_summary(
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
