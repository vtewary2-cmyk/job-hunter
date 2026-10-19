"""Minimize the Job Hunter Chrome window(s) so runs don't pop up over other apps.

Talks to the window over its debugging port (127.0.0.1:9222); a minimized
window keeps loading pages and filling forms normally.

    ~/JobHunter/app/.venv/bin/python ~/JobHunter/app/scripts/minimize_chrome.py
"""

from __future__ import annotations

import json
import urllib.request

from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"


def main() -> int:
    targets = json.load(urllib.request.urlopen(f"{CDP}/json/list", timeout=5))
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return 0
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        session = browser.new_browser_cdp_session()
        windows = set()
        for t in pages:
            try:  # a run opens and closes tabs constantly; skip ones already gone
                windows.add(session.send("Browser.getWindowForTarget", {"targetId": t["id"]})["windowId"])
            except Exception:
                continue
        for window_id in windows:
            session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "minimized"}})
        print(f"minimized {len(windows)} window(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
