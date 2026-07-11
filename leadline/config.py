"""Configuration (spec §13): env-driven defaults, with the AI backend settings
persisted to a settings.json so they are editable from the app's settings window
(a bundled .app has no shell environment)."""
import json
import os
from pathlib import Path

DATA_DIR = Path(
    os.getenv("LEADLINE_DATA_DIR", Path.home() / "Library" / "Application Support" / "LeadLine")
)
DB_PATH = DATA_DIR / "leadline.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "8"))
MAX_ANTHROPIC_DAILY_ARTICLES = int(os.getenv("MAX_ANTHROPIC_DAILY_ARTICLES", "2000"))
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
BODY_TEXT_CACHE_TTL_HOURS = int(os.getenv("BODY_TEXT_CACHE_TTL_HOURS", "24"))
PAYWALL_WORD_THRESHOLD = int(os.getenv("PAYWALL_WORD_THRESHOLD", "200"))

# User-editable settings; env vars provide the defaults, settings.json wins.
_DEFAULTS = {
    "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    "ollama_model": os.getenv("OLLAMA_MODEL", "phi4:14b"),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
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
