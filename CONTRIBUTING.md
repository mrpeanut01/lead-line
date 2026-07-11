# Contributing to LeadLine

Thanks for your interest! LeadLine is deliberately a **straightforward, no-frills**
implementation — the bar for adding code is "does the reader need this to read the news
faster," and the bar for adding a dependency is higher still.

## Dev setup

```bash
git clone https://github.com/mrpeanut01/lead-line.git
cd LeadLine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m leadline
```

Requires macOS (the window is pywebview on WKWebView) and Python 3.12+.

## Architecture in one minute

Single process, three moving parts:

```
leadline/
  config.py    env defaults + settings.json (user-editable at runtime)
  store.py     SQLite: articles, feed_sources; TTL purge of body text
  ingest.py    RSS poll (conditional GET) → dedup hash → extraction (stdlib HTMLParser)
  ai.py        prompt + router: primary/secondary servers (Ollama, Anthropic)
  api.py       pywebview JS bridge — every UI capability is a method here
  app.py       entry point: background pipeline thread + window
  ui/index.html  the whole UI: vertical snap-scroll card stack, settings overlay
```

Design rules worth knowing before you patch:

- **Summarize on demand only.** Articles are summarized when they enter the reader's
  read-ahead window (`request_summaries`), never in bulk from the pipeline.
- **Bodies are transient.** Extracted article text is TTL-purged (default 24 h); only
  summaries and headlines persist. Don't add long-term full-text storage.
- **Respect publishers.** robots.txt stays on; attribution stays prominent.
- **No frameworks.** The UI is one hand-written HTML file; the backend is stdlib +
  `pywebview`, `feedparser`, `requests`. Propose new dependencies in an issue first.

## Testing

There is no formal test suite yet (contributions welcome). Verify changes by:

1. Running the pipeline headless — poll/extract/summarize functions are all importable
   and take plain dicts; point `LEADLINE_DATA_DIR` at a scratch directory.
2. Launching the app with `LEADLINE_SELFTEST=1` — it renders the stack, reports
   `SELFTEST cards=N errors=[...]` to stdout, and exits non-interactively.
3. For AI-path changes, a tiny mock HTTP server for `/api/tags` and `/api/chat` is the
   established pattern (see PR history).

## Secrets

Never commit API keys, `settings.json`, or `*.db` files. User settings (including the
Anthropic key) live in `~/Library/Application Support/LeadLine/settings.json`, outside the
repo, chmod 600. CI/history is scanned before releases; keep it that way.

## Pull requests

- One focused change per PR, with the "why" in the description.
- Match the existing style: small modules, docstrings that cite the spec section they
  implement, no comment noise.
- Update `README.md` if you change settings, controls, or behavior.
- Releases: bump `leadline/__init__.__version__`, run `./build_app.sh`, zip
  `dist/LeadLine.app`, and attach it to a tagged GitHub release.
