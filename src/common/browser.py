"""Drive a dedicated, already-logged-in Chrome profile.

Session strategy: one Chrome profile at ``$JOBHUNTER_HOME/chrome`` that the user
signs into once.  Playwright reuses it on every run, so there are no stored
credentials, no automated login, and no CAPTCHA loop.  That deleted the Fernet
vault, all eight per-platform ``login()`` methods, and the re-login retry paths.

Two constraints shaped the mechanism, both discovered by testing rather than
assumption:

1. **Chrome >= 136 refuses ``--remote-debugging-port`` on the default profile.**
   Deliberate hardening against cookie-stealing malware.  Chrome here is 150,
   so pointing at the user's everyday profile is not an option; a dedicated
   ``--user-data-dir`` is required either way.
   https://developer.chrome.com/blog/remote-debugging-port

2. **``connect_over_cdp`` used to be broken on this pairing, and no longer is.**
   Playwright 1.60 against Chrome 150 failed the handshake with
   ``Browser.setDownloadBehavior: Browser context management is not supported``.
   Re-tested 2026-08-11 against Chrome 151.0.7922.76 on a throwaway profile:
   the port opens, the handshake completes, and a page drives fine.  So
   attaching to an already-open window is viable again, which is what
   ``session()`` now prefers.

Consequence: the login window no longer has to be closed before a run.  It is
launched with a debugging port, and the poller attaches to it rather than
fighting it for the profile lock.  If no such window is open, Playwright
launches its own and closes it afterwards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from playwright.async_api import BrowserContext, async_playwright

from src.common import home

log = logging.getLogger(__name__)

CHROME_BIN: Final[str] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR: Final[Path] = home.chrome_dir()

# Loopback only, and only ever on the dedicated profile.  Any local process can
# drive a Chrome with an open debugging port, which is precisely why Chrome
# refuses the flag on your everyday profile — do not point this at that one.
DEBUG_PORT: Final[int] = 9222
DEBUG_HOST: Final[str] = "127.0.0.1"

# Far enough right to stay out of the way on a large display, but still on it.
# Fully off-screen positions are unrecoverable without Accessibility access,
# which macOS will not grant a headless script.
_WINDOW: Final[tuple[int, int, int, int]] = (60, 60, 1280, 860)


class BrowserUnavailable(RuntimeError):
    """Chrome is missing, or its profile is locked by another process."""


def profile_in_use() -> bool:
    """True if a Chrome process already holds the job-hunter profile.

    Playwright cannot open a profile another Chrome has locked, and the failure
    mode is an opaque timeout, so check first and say something useful.
    """
    result = subprocess.run(
        ["pgrep", "-f", f"user-data-dir={PROFILE_DIR}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def cdp_endpoint() -> str | None:
    """Return the CDP URL if an attachable Chrome is already up, else None.

    This is the "is a window already open?" check.  A Chrome started without
    ``--remote-debugging-port`` has no port to find, and one cannot be added to
    a running process — which is why ``open_for_login`` now passes the flag.
    """
    url = f"http://{DEBUG_HOST}:{DEBUG_PORT}"
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=1.5) as resp:
            log.debug("attachable chrome: %s", json.loads(resp.read()).get("Browser"))
        return url
    except (urllib.error.URLError, OSError, ValueError):
        return None


def open_for_login(url: str = "https://www.linkedin.com/login") -> None:
    """Open the profile in a normal Chrome window so the user can sign in.

    Not a Playwright launch: Google blocks sign-in from automation-flagged
    browsers, and this window is for a human anyway.  The session it
    establishes persists in the profile directory for every later run.

    The debugging port is what lets the poller reuse *this* window instead of
    launching a second browser, so leave it open.
    """
    if not Path(CHROME_BIN).exists():
        raise BrowserUnavailable(f"Chrome not found at {CHROME_BIN}")
    if cdp_endpoint() is not None:
        raise BrowserUnavailable(
            f"A job-hunter Chrome is already open on port {DEBUG_PORT}. "
            "Use that window to sign in — there is no need for a second one."
        )
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    x, y, w, h = _WINDOW
    subprocess.Popen(
        [
            CHROME_BIN,
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--remote-debugging-address={DEBUG_HOST}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-position={x},{y}",
            f"--window-size={w},{h}",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def _open_tabs_in_background(browser, context: BrowserContext) -> None:
    """Make context.new_page() open background tabs that never take focus.

    A normal new tab brings the Job Hunter window in front of whatever the
    candidate is watching in her own Chrome (2026-10-12). CDP's createTarget
    with background=true opens the tab without activating the window.
    """
    cdp = await browser.new_browser_cdp_session()
    windows = [p for p in context.pages if not p.url.startswith("devtools")]

    async def new_page():
        params = {"url": "about:blank", "background": True}
        if not windows:
            params["newWindow"] = True  # first tab of an empty profile
        async with context.expect_page() as info:
            await cdp.send("Target.createTarget", params)
        return await info.value

    context.new_page = new_page  # type: ignore[method-assign]


@asynccontextmanager
async def session(headless: bool = False):
    """Yield a Playwright context bound to the job-hunter profile.

    Prefers an already-open window.  If one is listening on the debugging port
    we attach to it and leave it running afterwards — it is the user's window,
    not ours to close.  Otherwise Playwright launches its own and tears it down.

    ``headless`` only applies to the launch path: the profile has to survive a
    Google OAuth flow, and Google blocks headless sign-in.  Once the session
    cookie exists, headless works for scraping — but a visible window also makes
    it obvious when something has gone wrong at 11am on a Tuesday.
    """
    if not Path(CHROME_BIN).exists():
        raise BrowserUnavailable(f"Chrome not found at {CHROME_BIN}")

    endpoint = cdp_endpoint()
    if endpoint is not None:
        log.info("attaching to the Chrome already open on %s", endpoint)
        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(endpoint)
        try:
            # contexts[0] is the profile's own context, cookies and all. A
            # new_context() here would be a fresh incognito-ish jar with no
            # LinkedIn session, which fails in a confusing way much later.
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            await _open_tabs_in_background(browser, context)
            yield context
        finally:
            # Detach only. Closing would take the user's window down with it.
            await playwright.stop()
        return

    if profile_in_use():
        raise BrowserUnavailable(
            f"A Chrome window has {PROFILE_DIR} open but is not attachable — it was "
            f"started without --remote-debugging-port, and a running process cannot "
            f"be given one. Close that window and run `python -m src.run_poller "
            f"--login`, which opens it with the port so every later run can reuse it."
        )

    x, y, w, h = _WINDOW
    playwright = await async_playwright().start()
    context: BrowserContext = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",  # real Chrome, not bundled Chromium — fewer bot signals
        headless=headless,
        viewport={"width": w, "height": h},
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-position={x},{y}",
        ],
        locale="en-US",
        timezone_id="Asia/Kolkata",
    )
    try:
        yield context
    finally:
        await context.close()
        await playwright.stop()


async def is_logged_in_linkedin(context: BrowserContext) -> bool:
    """Cheap check that the profile still holds a LinkedIn session.

    Waits for the document, not the full "load" event, and tries twice: the
    feed's load event can take over 30s (worst just after the Mac wakes), and a
    timeout here used to end the whole run as "session is dead" while the
    session was fine (2026-10-09 and 2026-10-11).
    """
    for attempt in (1, 2):
        page = await context.new_page()
        try:
            await page.goto("https://www.linkedin.com/feed/", timeout=60_000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            url = page.url
            if "/login" in url or "authwall" in url or "/checkpoint" in url:
                return False  # LinkedIn really sent us to sign in
            if "/feed" in url:
                return True
        except Exception as exc:
            log.warning("linkedin session check attempt %d failed: %s", attempt, exc)
        finally:
            await page.close()
    # Only a network problem gets here: fall back to the login cookie itself.
    cookies = await context.cookies("https://www.linkedin.com")
    return any(c["name"] == "li_at" for c in cookies)


def linkedin_signed_in() -> bool:
    """True if the open job-hunter Chrome holds LinkedIn's ``li_at`` login cookie.

    Reads cookies over the debugging port, so it needs no page load and works
    while the person is still on the login screen — the setup page polls it.
    Sync API on purpose: the setup server is a plain thread, not an event loop.
    """
    endpoint = cdp_endpoint()
    # Port 9222 is machine-wide: make sure the Chrome on it is *this* home's
    # profile, not another candidate's (seen when testing a handoff zip on
    # the developer's Mac, where his own signed-in window answered).
    if endpoint is None or not profile_in_use():
        return False
    from playwright.sync_api import Error, sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(endpoint)
            contexts = browser.contexts
            cookies = contexts[0].cookies("https://www.linkedin.com") if contexts else []
            return any(c["name"] == "li_at" for c in cookies)
    except Error as exc:
        log.debug("cookie check failed: %s", exc)
        return False
