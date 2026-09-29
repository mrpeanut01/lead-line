"""LeadLine entry point: background pipeline worker + native Mac window."""
import os
import sys
import threading
import time
from pathlib import Path

import webview

from . import ai, config, store
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
        # ticker round trip: the strip shows headlines or, with no Ollama, an error
        window.evaluate_js("enterTicker()")
        time.sleep(4)
        strip = window.height
        ticker = window.evaluate_js(
            "document.body.classList.contains('ticker') && "
            "(document.querySelectorAll('#ticker-track .tk').length > 0 || "
            "!!document.getElementById('ticker-note').textContent)")
        window.evaluate_js("exitTicker()")
        time.sleep(2)
        errs = window.evaluate_js("window.__errs || []")
        ok = ticker and strip < 200 and window.height >= 640
        print(f"SELFTEST cards={cards} ticker={'ok' if ok else 'FAIL'} "
              f"errors={errs}", flush=True)
    except Exception as e:
        print(f"SELFTEST FAIL: {e}", flush=True)
    window.destroy()


def main():
    store.seed_default_feeds()
    api = Api()
    threading.Thread(target=pipeline_worker, args=(api,), daemon=True).start()
    threading.Thread(target=ai.monitor_ollama, daemon=True).start()   # warm the model now

    window = webview.create_window(
        "LeadLine",
        url=str(UI_INDEX),
        js_api=api,
        width=540,
        height=900,
        # small enough for the ticker strip; the reader restores its own size
        min_size=(360, 60),
        background_color="#faf7f2",
    )
    api._window = window
    if os.getenv("LEADLINE_SELFTEST"):
        threading.Thread(target=_selftest, args=(window,), daemon=True).start()
        webview.start(lambda: window.evaluate_js(
            "window.__errs=[];window.onerror=(m)=>{window.__errs.push(String(m))}"))
    else:
        webview.start()


if __name__ == "__main__":
    main()
