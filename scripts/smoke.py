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

# summary validation
good = ('{"straight_headline": "H", "bluf_bullets": ["a", "b", "c"], "one_sentence": "s",'
        ' "topic_tags": ["t"], "confidence": 2}')
assert ai._parse_summary("noise " + good)["confidence"] == 1.0
try:
    ai._parse_summary('{"straight_headline": "H", "bluf_bullets": ["a"], "one_sentence": "s"}')
    raise SystemExit("short bullet list accepted")
except ValueError:
    pass

# summary queue: most urgent first, done reported, late request ignored
article = queue[0]
done = []
ai.ensure_body = lambda article_id: None
ai.note_demand = lambda: None
ai.summarize_article = lambda article_id, epoch=None: done.append(article_id)
bridge = api.Api()
states = bridge.request_summaries([article["id"], "missing"], 2)["states"]
assert set(states) == {article["id"], "missing"}
for _ in range(50):
    if len(done) == 2:
        break
    time.sleep(0.1)
assert done == [article["id"], "missing"], done
assert bridge.request_summaries([article["id"]], 1)["seq"] == 2   # older call changes nothing
assert bridge.request_summaries([article["id"]], 3)["states"][article["id"]]["state"] == "done"

print("smoke ok")
