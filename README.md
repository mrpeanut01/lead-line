# LeadLine

An anti-clickbait, BLUF-first news reader for the Mac — a straightforward implementation of the
**UpFront News Reader** product spec.

Every story is shown one at a time in a full-window **vertical snap-scroll stack**: an
AI-rewritten straight headline plus 3–5 Bottom-Line-Up-Front bullets, always above the fold.
Scroll down for the next story. Tap **Read full article** to expand the extracted body inline;
**Source ↗** opens the publisher in your browser.

## Architecture

Single-process Python desktop app in a native macOS window (pywebview / WKWebView):

| Spec layer | Implementation |
|---|---|
| Ingestion | RSS polling with conditional GET (ETag / Last-Modified), URL normalization, SHA-256 dedup, `INSERT OR IGNORE` |
| Extraction | Dependency-free paragraph harvester + `og:image`; robots.txt respected; paywall detection by word count |
| AI processing | Ollama first (structured JSON output), Anthropic Claude Haiku fallback with a hard daily cap; provider logged per article |
| Storage | SQLite in `~/Library/Application Support/LeadLine/` (Postgres stand-in); body text TTL-purged after 24 h (Redis stand-in) |
| UI | `scroll-snap-type: y mandatory` card stack, 100vh cards, edge progress bar, topic accent, inline reading mode |

Only summaries and headlines persist long-term — extracted body text is transient (spec §12).

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m leadline
```

Without any configuration the app works immediately: stories appear with their original
headlines, and BLUF summaries fill in as an AI backend becomes available.

## Build the .app

```bash
.venv/bin/pip install pyinstaller
./build_app.sh          # produces dist/LeadLine.app (ad-hoc signed)
cp -R dist/LeadLine.app /Applications/
```

## AI backends

Configure both backends in-app under **⚙ → AI Backends**: the Ollama server URL, the
Anthropic API key, and the model for each — with one-click discovery of the models your
Ollama server has installed and the models your Anthropic key can access. Settings persist
to `~/Library/Application Support/LeadLine/settings.json` (mode 600) and take effect
immediately; environment variables below act as defaults only.

- **Ollama (primary):** install from <https://ollama.com>, then e.g. `ollama pull llama3.2:3b`
  and pick it in settings.
- **Anthropic (fallback):** paste an API key from <https://console.anthropic.com>.

## Configuration (environment variables)

| Variable | Default |
|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` |
| `OLLAMA_MODEL` | `phi4:14b` |
| `OLLAMA_TIMEOUT_SECONDS` | `8` |
| `ANTHROPIC_API_KEY` | (unset — fallback disabled) |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` |
| `MAX_ANTHROPIC_DAILY_ARTICLES` | `2000` |
| `POLL_INTERVAL_MINUTES` | `15` |
| `BODY_TEXT_CACHE_TTL_HOURS` | `24` |
| `PAYWALL_WORD_THRESHOLD` | `200` |
| `LEADLINE_DATA_DIR` | `~/Library/Application Support/LeadLine` |

## Controls

| Input | Action |
|---|---|
| Scroll / swipe up, `↓` `j` space | Next story (marks the passed story read) |
| Scroll / swipe down, `↑` `k` | Previous story |
| Read full article | Expand body inline |
| ⟳ | Poll feeds now |
| ⚙ | Manage RSS sources, view provider stats |

Default sources: NPR, PBS NewsHour, Ars Technica, The Verge, Wired — add or remove any RSS feed
in ⚙.
