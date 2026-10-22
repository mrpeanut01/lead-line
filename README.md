# LeadLine

An anti-clickbait, BLUF-first news reader for Mac and Windows — a straightforward
implementation of the **UpFront News Reader** product spec.

Every story is shown one at a time in a full-window **vertical snap-scroll stack**: an
AI-rewritten straight headline plus 3–5 Bottom-Line-Up-Front bullets, always above the fold.
Scroll down for the next story. Tap **Read full article** to expand the extracted body inline;
**Source ↗** opens the publisher in your browser. The publisher's original headline stays
visible (subordinate, in the card footer) for transparency. A story reported several times —
by several outlets, or as a review plus its photo galleries — is **one card** with one
combined summary, and articles with no news in them are skipped.

Privacy-first by design: everything runs locally in a single process. No accounts, no
telemetry, no cloud backend — article text goes only to the AI server *you* configure
(your own Ollama, or Anthropic with your key), and only for the story you're reading plus
a small read-ahead window.

## Install

### macOS

Download `LeadLine-<version>-mac.dmg` from [Releases](https://github.com/mrpeanut01/lead-line/releases),
open it, and drag `LeadLine.app` onto the Applications shortcut. (A `LeadLine-<version>-mac.zip`
of the same app is attached too.)

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
| Ingestion | RSS polling with conditional GET (ETag / Last-Modified), URL normalization, SHA-256 dedup, `INSERT OR IGNORE`; each new article joins the story it reports (rarity-weighted headline similarity, see [One card per story](#one-card-per-story)) |
| Extraction | Dependency-free paragraph harvester + `og:image`; robots.txt respected (a robots.txt that can't be read means the page isn't fetched); paywall detection by word count; a site that is down is retried before falling back to the feed's description |
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
`LeadLine-<version>-mac.dmg`, `LeadLine-<version>-mac.zip` and `LeadLine-<version>-windows.zip` to
the GitHub release. Run it
on demand from the Actions tab (**Run workflow**) to get artifacts without a release.

Local builds — macOS:

```bash
.venv/bin/pip install pyinstaller
./build_app.sh          # produces dist/LeadLine.app (ad-hoc signed) plus versioned .zip and .dmg
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
  doesn't need it, and it multiplies latency); models that reason anyway, like gpt-oss, are
  asked for the least reasoning instead, and models that reject the setting are called
  normally. What works is remembered per model for the session.
- **Anthropic:** paste an API key from <https://console.anthropic.com>.

Stories are summarized **on demand only** — the story on screen plus the **Read ahead**
window (0–10, default 1). Nothing is sent to a model before you're about to read it, and a
story you scroll past stops being summarized so the one on screen goes next. A story's
summary covers every report of it (see [One card per story](#one-card-per-story)). Two
things send more than that, and only to your Ollama server, never Claude: the ticker's
one-line headlines, and the same-story check, which sends the headlines and feed
descriptions (no article text) of up to six unread stories near the one being summarized,
even ones beyond the read-ahead window.

Summaries stick to the article. The model is told when the article was published and
today's date, and must not add a year, age or figure from its own memory. A model left to
guess assumes the year its training ended in, so a 2026 story would come back as "the 2024
season". A summary that still states a year or age the article never mentions is asked
for once more with a correction, and any bullets still stating it are dropped.

A card that is still waiting shows where its summary stands — waiting its turn, summarizing
now, or failed (with the reason) — under the original headline. A failed story is retried
on its own, at longer intervals each time; **↻ Retry summary** on the card resubmits it at
once and re-checks the AI servers.

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

## One card per story

Feeds repeat themselves: three outlets report the same court ruling, and a car site posts a
review with an interior gallery, an exterior gallery and a spec page for the same car.
LeadLine shows each story once.

- **Grouped as it arrives.** Each new article is compared with the last two days of
  headlines. Words count by how rare they are, so a shared model name or place weighs far
  more than a shared "Apple" or "Trump". A close match joins that story. This happens at
  poll time, without a model.
- **Closer calls go to Ollama.** Just before a story is summarized, unread stories that come
  close to it — similar headlines, a shared name like "iPhone Duo", or the same site posting
  at the same moment — are put to your Ollama model as one short question: which of these
  are the same news event or the same specific product? A shared company, person or topic
  isn't enough, and roundups, podcasts and deals posts never count. The ones it confirms
  join the story. Like the ticker, this is Ollama only: those stories' headlines and feed
  descriptions never go to Claude, and while Ollama is off or still loading, the check is
  skipped and they stay cards of their own.
- **One combined summary.** The card's summary is written from up to four of the story's
  reports, what they agree on first and then what only some of them add. The source bar shows
  **+N more reports**, and **N reports on this story** under the bullets lists them all,
  each opening at its source.
- **Read once.** Scrolling past the card reads every report of it, and a report of the same
  story that arrives later comes in already read.
- **No news, no card.** When the AI finds nothing to report — a podcast episode listing, a
  sponsor message, a quiz, a lone quote — the story is skipped and its card leaves the stack
  (a short note says so if it was the one on screen). If it led a story, the next best report
  leads it instead.

The first launch after updating hides old summaries that already said there was no news,
drops unread summaries that state a year or age their article never mentions (they are made
again, with the date check, when you reach them), and groups the last week's stories, once. Settings shows how many reports were combined and
how many were skipped.

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

## Sharing a story

**Share** on a summarized card (and beside **Source ↗** when the article is open) copies the
story to your clipboard as plain text, ready to paste into Messages, Mail, Slack or anything
else:

```text
Fed holds rates at 4.25% and signals two cuts before year end

• The Federal Reserve left its benchmark rate unchanged on Wednesday.
• Officials projected two quarter-point cuts by December.
• Chair Powell cited cooling inflation but a still-tight labor market.

Original headline (NPR): “The Fed just made a move that could change everything for your wallet”
https://www.npr.org/2026/10/20/fed-rates
```

The layout is built for iMessage. It is plain text, because Messages shows Markdown as literal
asterisks, and the link sits alone on the last line, so Messages sends it as its own bubble
with a preview card. Slack and most other chat apps unfurl a trailing link the same way.

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
| Share | Copy the summary, original headline and link, ready to paste (see above) |
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
