"""LeadLine entry point: background pipeline worker + native Mac window."""
import os
import sys
import threading
import time
from pathlib import Path

import webview

from . import config, store
from .api import Api

if getattr(sys, "frozen", False):  # PyInstaller bundle
    UI_INDEX = Path(sys._MEIPASS) / "leadline" / "ui" / "index.html"
else:
    UI_INDEX = Path(__file__).parent / "ui" / "index.html"


def pipeline_worker(api):
    """Poll on the configured schedule (spec §5.1, default 15 min)."""
    while True:
        try:
            api.refresh()
        except Exception as e:
            print(f"[pipeline] error: {e}")
        time.sleep(config.POLL_INTERVAL_MINUTES * 60)


def _selftest(window):
    """LEADLINE_SELFTEST=1: probe the rendered DOM, report, and exit."""
    time.sleep(10)
    try:
        cards = window.evaluate_js("document.querySelectorAll('.card').length")
        errs = window.evaluate_js("window.__errs || []")
        print(f"SELFTEST cards={cards} errors={errs}", flush=True)
    except Exception as e:
        print(f"SELFTEST FAIL: {e}", flush=True)
    window.destroy()


def main():
    store.seed_default_feeds()
    api = Api()
    threading.Thread(target=pipeline_worker, args=(api,), daemon=True).start()

    window = webview.create_window(
        "LeadLine",
        url=str(UI_INDEX),
        js_api=api,
        width=540,
        height=900,
        min_size=(420, 640),
        background_color="#faf7f2",
    )
    if os.getenv("LEADLINE_SELFTEST"):
        threading.Thread(target=_selftest, args=(window,), daemon=True).start()
        webview.start(lambda: window.evaluate_js(
            "window.__errs=[];window.onerror=(m)=>{window.__errs.push(String(m))}"))
    else:
        webview.start()


if __name__ == "__main__":
    main()
