"""Regenerate leadline/ui/catalog.json from plenaryapp/awesome-rss-feeds.

Run occasionally (network required); the output is committed and bundled so
the app never fetches the catalog at runtime.

    .venv/bin/python scripts/build_catalog.py
"""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import requests

REPO_RAW = "https://raw.githubusercontent.com/plenaryapp/awesome-rss-feeds/master"
REPO_API = "https://api.github.com/repos/plenaryapp/awesome-rss-feeds/contents"
OUT = Path(__file__).parent.parent / "leadline" / "ui" / "catalog.json"


def list_opml(folder):
    resp = requests.get(f"{REPO_API}/{quote(folder)}", timeout=30)
    resp.raise_for_status()
    return [f["name"] for f in resp.json() if f["name"].endswith(".opml")]


def parse_opml(folder, filename):
    resp = requests.get(f"{REPO_RAW}/{quote(folder)}/{quote(filename)}", timeout=30)
    resp.raise_for_status()
    # some files contain raw ampersands, which are not well-formed XML
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", resp.text)
    feeds = []
    for outline in ET.fromstring(text).iter("outline"):
        url = outline.get("xmlUrl")
        name = outline.get("title") or outline.get("text")
        if url and name:
            feeds.append({"name": name.strip(), "url": url.strip()})
    return feeds


def build_group(folder):
    groups = []
    for filename in sorted(list_opml(folder), key=str.lower):
        category = filename[:-5]  # strip .opml
        try:
            feeds = parse_opml(folder, filename)
        except ET.ParseError as e:
            print(f"  {category}: SKIPPED (bad XML: {e})")
            continue
        if feeds:
            groups.append({"name": category, "feeds": feeds})
            print(f"  {category}: {len(feeds)} feeds")
    return groups


def main():
    print("Topics:")
    topics = build_group("recommended/with_category")
    print("Countries:")
    countries = build_group("countries/with_category")
    total = sum(len(g["feeds"]) for g in topics + countries)
    OUT.write_text(json.dumps({"topics": topics, "countries": countries},
                              indent=1, ensure_ascii=False))
    print(f"Wrote {OUT} — {total} feeds in {len(topics)} topics + {len(countries)} countries")


if __name__ == "__main__":
    main()
