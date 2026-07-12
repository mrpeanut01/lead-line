"""Configuration (spec §13): env-driven defaults, with the AI backend settings
persisted to a settings.json so they are editable from the app's settings window
(a bundled .app has no shell environment)."""
import json
import os
import sys
from pathlib import Path


def _default_data_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LeadLine"
    if os.name == "nt":
        return Path(os.getenv("APPDATA", Path.home())) / "LeadLine"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "LeadLine"


DATA_DIR = Path(os.getenv("LEADLINE_DATA_DIR", _default_data_dir()))
DB_PATH = DATA_DIR / "leadline.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "8"))
MAX_ANTHROPIC_DAILY_ARTICLES = int(os.getenv("MAX_ANTHROPIC_DAILY_ARTICLES", "2000"))
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
BODY_TEXT_CACHE_TTL_HOURS = int(os.getenv("BODY_TEXT_CACHE_TTL_HOURS", "24"))
PAYWALL_WORD_THRESHOLD = int(os.getenv("PAYWALL_WORD_THRESHOLD", "200"))

# User-editable settings; env vars provide the defaults, settings.json wins.
# *_role: "primary" | "secondary" | "off" — order the AI router tries servers.
# read_ahead: stories beyond the current card to summarize in advance (0-10).
# max_story_age_days: stories older than this are not offered in the queue (1-30).
_DEFAULTS = {
    "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    "ollama_model": os.getenv("OLLAMA_MODEL", "phi4:14b"),
    "ollama_role": os.getenv("OLLAMA_ROLE", "primary"),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
    "anthropic_role": os.getenv("ANTHROPIC_ROLE", "secondary"),
    "read_ahead": int(os.getenv("READ_AHEAD", "1")),
    "max_story_age_days": int(os.getenv("MAX_STORY_AGE_DAYS", "3")),
}


def load_settings():
    settings = dict(_DEFAULTS)
    try:
        settings.update(json.loads(SETTINGS_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(updates):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = {}
    try:
        current = json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        pass
    current.update({k: v for k, v in updates.items() if k in _DEFAULTS})
    try:
        current["read_ahead"] = max(0, min(10, int(current.get("read_ahead", 1))))
    except (TypeError, ValueError):
        current["read_ahead"] = 1
    try:
        current["max_story_age_days"] = max(1, min(30, int(current.get("max_story_age_days", 3))))
    except (TypeError, ValueError):
        current["max_story_age_days"] = 3
    for k in ("ollama_role", "anthropic_role"):
        if current.get(k) not in ("primary", "secondary", "off"):
            current[k] = _DEFAULTS[k]
    SETTINGS_PATH.write_text(json.dumps(current, indent=2))
    SETTINGS_PATH.chmod(0o600)  # holds the API key
    return load_settings()


def setting(key):
    return load_settings()[key]


DEFAULT_FEEDS = [
    ("NPR News", "https://feeds.npr.org/1001/rss.xml"),
    ("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Wired", "https://www.wired.com/feed/rss"),
]
