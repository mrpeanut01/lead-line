"""Environment-driven configuration (spec §13)."""
import os
from pathlib import Path

DATA_DIR = Path(
    os.getenv("LEADLINE_DATA_DIR", Path.home() / "Library" / "Application Support" / "LeadLine")
)
DB_PATH = DATA_DIR / "leadline.db"

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi4:14b")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "8"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
MAX_ANTHROPIC_DAILY_ARTICLES = int(os.getenv("MAX_ANTHROPIC_DAILY_ARTICLES", "2000"))

POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
BODY_TEXT_CACHE_TTL_HOURS = int(os.getenv("BODY_TEXT_CACHE_TTL_HOURS", "24"))
PAYWALL_WORD_THRESHOLD = int(os.getenv("PAYWALL_WORD_THRESHOLD", "200"))

DEFAULT_FEEDS = [
    ("NPR News", "https://feeds.npr.org/1001/rss.xml"),
    ("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Wired", "https://www.wired.com/feed/rss"),
]
