"""LeadLine entry point: background pipeline worker + native Mac window."""
import threading
import time
from pathlib import Path

import webview

from . import config, store
from .api import Api

UI_INDEX = Path(__file__).parent / "ui" / "index.html"


def pipeline_worker(api):
    """Poll on the configured schedule (spec §5.1, default 15 min)."""
    while True:
        try:
            api.refresh()
        except Exception as e:
            print(f"[pipeline] error: {e}")
        time.sleep(config.POLL_INTERVAL_MINUTES * 60)


def main():
    store.seed_default_feeds()
    api = Api()
    threading.Thread(target=pipeline_worker, args=(api,), daemon=True).start()

    webview.create_window(
        "LeadLine",
        url=str(UI_INDEX),
        js_api=api,
        width=540,
        height=900,
        min_size=(420, 640),
        background_color="#faf7f2",
    )
    webview.start()


if __name__ == "__main__":
    main()
