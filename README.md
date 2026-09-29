# LeadLine

An anti-clickbait, BLUF-first news reader for Mac and Windows — a straightforward
implementation of the **UpFront News Reader** product spec.

Every story is shown one at a time in a full-window **vertical snap-scroll stack**: an
AI-rewritten straight headline plus 3–5 Bottom-Line-Up-Front bullets, always above the fold.
Scroll down for the next story. Tap **Read full article** to expand the extracted body inline;
**Source ↗** opens the publisher in your browser. The publisher's original headline stays
visible (subordinate, in the card footer) for transparency.

Privacy-first by design: everything runs locally in a single process. No accounts, no
telemetry, no cloud backend — article text goes only to the AI server *you* configure
(your own Ollama, or Anthropic with your key), and only for the story you're reading plus
a small read-ahead window.

## Install

### macOS

Download `LeadLine-<version>-mac.zip` from [Releases](https://github.com/mrpeanut01/lead-line/releases),
unzip, and drag `LeadLine.app` to Applications.

> **Gatekeeper note:** the app is ad-hoc signed (not notarized), so on first launch macOS
> will warn you. Right-click the app → **Open** → **Open**, or clear the quarantine flag:
> `xattr -dr com.apple.quarantine /Applications/LeadLine.app`.

### Windows

Download `LeadLine-<version>-windows.zip` from
[Releases](https://github.com/mrpeanut01/lead-line/releases), unzip, and run `LeadLine.exe`.
Requires the [Edge WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
(preinstalled on Windows 11 and on up-to-date Windows 10). Data lives in `%APPDATA%\LeadLine`.

> **SmartScreen note:** the exe is unsigned, so Windows may warn on first launch —
> click **More info** → **Run anyway**.

## Architecture

Single-process Python desktop app in a native macOS window (pywebview / WKWebView):

| Spec layer | Implementation |
|---|---|
| Ingestion | RSS polling with conditional GET (ETag / Last-Modified), URL normalization, SHA-256 dedup, `INSERT OR IGNORE` |
| Extraction | Dependency-free paragraph harvester + `og:image`; robots.txt respected; paywall detection by word count |
| AI processing | Ollama first (structured JSON output, model warmed into memory at launch), Anthropic Claude Haiku fallback — or cover while the model loads — with a hard daily cap; provider logged per article |
| Storage | SQLite in `~/Library/Application Support/LeadLine/` (Postgres stand-in); body text TTL-purged after 24 h (Redis stand-in) |
| UI | `scroll-snap-type: y mandatory` card stack, 100vh cards, edge progress bar, topic accent, inline reading mode |

Only summaries and headlines persist long-term — extracted body text is transient (spec §12).

## Run from source

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m leadline
```

Without any configuration the app works immediately: stories appear with their original
headlines, and BLUF summaries fill in as an AI backend becomes available.

## Build the .app / .exe

Releases are built by CI ([build-release.yml](.github/workflows/build-release.yml)): every
`v*` tag builds `LeadLine.app` on a macOS runner and `LeadLine.exe` on a Windows runner
(PyInstaller can't cross-compile), smoke-tests both binaries, and attaches
`LeadLine-<version>-mac.zip` and `LeadLine-<version>-windows.zip` to the GitHub release. Run it
on demand from the Actions tab (**Run workflow**) to get artifacts without a release.

Local builds — macOS:

```bash
.venv/bin/pip install pyinstaller
./build_app.sh          # produces dist/LeadLine.app (ad-hoc signed, versioned)
cp -R dist/LeadLine.app /Applications/
```

Windows:

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean LeadLine-win.spec   # produces dist/LeadLine.exe
```

## AI backends

Configure both backends in-app under **⚙ → AI Backends**: the Ollama server URL, the
Anthropic API key, and the model for each — with one-click discovery of the models your
Ollama server has installed and the models your Anthropic key can access. Each server has a
role — **Primary**, **Secondary**, or **Off** — and the router tries them in that order
(promoting one to Primary demotes the other). Settings persist to
`~/Library/Application Support/LeadLine/settings.json` (mode 600) and take effect
immediately; environment variables below act as defaults only.

- **Ollama:** install from <https://ollama.com>, then e.g. `ollama pull gemma4:12b`
  and pick it in settings. Thinking models are asked to skip their reasoning pass (a summary
  doesn't need it, and it multiplies latency); models that can't skip it, like gpt-oss, are
  called normally.
- **Anthropic:** paste an API key from <https://console.anthropic.com>.

Stories are summarized **on demand only** — the story on screen plus the **Read ahead**
window (0–10, default 1). Nothing is sent to a model before you're about to read it.

### Ollama model loading

A local model has to be loaded into memory before it can answer, and a cold load plus a
summary can take longer than the summary alone. LeadLine loads the Ollama model as soon as it
launches, and again when you come back to reading after the server has unloaded it — so
summaries wait for the load instead of timing out. Claude has an extra role for this:

- **Secondary** — summaries wait while the model loads; Claude is used only if Ollama fails
  or is unreachable.
- **Primary during Ollama load, then Secondary** — Claude summarizes until the model is in
  memory, then steps back to fallback. No waiting on a cold model.

The status pill at the top of the window shows both servers (hover for details, click for
settings):

| Dot | Ollama | Claude |
|---|---|---|
| green | model loaded and ready | summarizing (primary, or covering for Ollama) |
| green ring | — | standing by as the fallback |
| amber, pulsing | loading the model into memory | — |
| amber ring | model not in memory; loads when you read | — |
| amber | loaded, but the last summary failed | — |
| red | server unreachable, or model not installed | no API key, or the last request failed |
| grey | off | off |

## Ticker view

Press **▬** (or `t`) to minimize LeadLine into a news ticker: a thin strip that stays on
top of other windows and scrolls the newest stories as wire-style lines of eight words or
fewer. Drag the window edge to make it as wide as you like.

- **Ollama only.** Ticker lines are written by your Ollama model and nothing else; Claude is
  never used for them, whatever its role. Each line is condensed from the story's headline
  and feed description, so the ticker never fetches or summarizes full articles. Stories
  Ollama hasn't condensed yet stay off the tape rather than showing the original headline.
- **Errors in the strip.** If Ollama is off, unreachable, or missing the model, the strip
  says so in red (click it to open settings). While the model loads, an amber note shows how
  many stories are waiting.
- **Fresh to faded.** A new story is drawn in the scheme's highlight color and fades to
  neutral over **Fade to neutral** (default 2 hours); it leaves the tape after **Remove
  after** (default 6 hours). The tape holds the newest **Stories** (default 10).
- **Time on screen follows the content.** The tape moves at a constant reading speed, so a
  longer line stays in view longer. Hover to pause.
- **Keeps itself current.** While the ticker is open it polls your feeds every **Check feeds
  every** minutes (default 5, minimum 2) and picks up new lines as Ollama writes them.
- **Click a story** to return to the full view with its AI summary card open. **⤢**, a
  double-click, or `Esc` returns to the full view; **⚙** opens the ticker's settings.

Customize it under **⚙ → Ticker**: number of stories, fade and removal times, poll cadence,
scroll speed, text size, color scheme (Paper, Night, Amber terminal, Green terminal), and
always-on-top.

## Configuration (environment variables)

| Variable | Default |
|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` |
| `OLLAMA_MODEL` | `phi4:14b` |
| `OLLAMA_TIMEOUT_SECONDS` | `120` (one summary, once the model is loaded) |
| `OLLAMA_LOAD_TIMEOUT_SECONDS` | `300` (loading the model into memory) |
| `OLLAMA_ROLE` | `primary` |
| `ANTHROPIC_API_KEY` | (unset — fallback disabled) |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` |
| `ANTHROPIC_ROLE` | `secondary` (or `primary`, `primary_during_load`, `off`) |
| `READ_AHEAD` | `1` |
| `MAX_STORY_AGE_DAYS` | `3` |
| `MAX_ANTHROPIC_DAILY_ARTICLES` | `2000` |
| `POLL_INTERVAL_MINUTES` | `15` |
| `BODY_TEXT_CACHE_TTL_HOURS` | `24` |
| `PAYWALL_WORD_THRESHOLD` | `200` |
| `LEADLINE_DATA_DIR` | `~/Library/Application Support/LeadLine` (macOS) / `%APPDATA%\LeadLine` (Windows) |

## Controls

| Input | Action |
|---|---|
| Scroll / swipe up, `↓` `j` space | Next story (marks the passed story read) |
| Scroll / swipe down, `↑` `k` | Previous story |
| Read full article | Expand body inline |
| ▬ or `t` | Minimize to the ticker view (see above) |
| `Esc`, ⤢, or double-click the strip | Leave the ticker view |
| ⟳ (or **Check for new stories** on the last card) | Poll feeds now and jump back to the top if anything is new; a toast says when nothing is |
| ● Ollama ● Claude | AI server status (see above); click for settings |
| ⚙ | Manage RSS sources, view provider stats |

Default sources: NPR, PBS NewsHour, Ars Technica, The Verge, Wired. In ⚙ you can add any RSS
URL directly, or browse the built-in catalog — a bundled snapshot of
[awesome-rss-feeds](https://github.com/plenaryapp/awesome-rss-feeds) with ~800 curated feeds
across 41 topics and 24 countries (regenerate with `scripts/build_catalog.py`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, architecture, and PR guidelines.

## Rights & attribution

LeadLine is a personal reader built to be conservative about publisher content: bodies are
extracted transiently for summarization and TTL-purged within 24 hours, robots.txt is
respected, every card attributes and links to the publisher, and only AI-generated summaries
persist. See the spec's legal considerations before deploying beyond personal use.
