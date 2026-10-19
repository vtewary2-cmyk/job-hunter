"""The local setup page a candidate sees after double-clicking the app.

    python -m src.setup_app.server        # serves http://127.0.0.1:8765 and opens it

Stdlib only.  Bound to 127.0.0.1, so nothing on the network can reach it.
Each card on the page maps to one endpoint:

    1. Email + notifications   POST /profile   -> $JOBHUNTER_HOME/.env
    2. Sign in to LinkedIn     POST /login     -> opens the Job Hunter Chrome
    3. Connect Google          POST /google    -> OAuth, creates the Sheet
    4. Start daily 9 AM        POST /schedule  -> launchd + wake + first Sheet sync
    GET /state reports all four, and the page polls it.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

from src.common import browser, data_store, home
from src.notify import google_auth, sheet

log = logging.getLogger(__name__)

PORT: Final[int] = 8765
_PAGE: Final[Path] = Path(__file__).with_name("index.html")
_google_error: dict[str, str] = {}


def read_env() -> dict[str, str]:
    path = home.env_path()
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def write_env(updates: dict[str, str]) -> None:
    env = {**read_env(), **{k: v for k, v in updates.items() if v}}
    path = home.env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    path.chmod(0o600)


def _profile() -> dict:
    try:
        return json.loads(home.form_profile_path().read_text())
    except (OSError, ValueError):
        return {}


def state() -> dict:
    from src import config
    from src.setup_app import scheduler

    env = read_env()
    settings = config.load()
    return {
        "name": _profile().get("name", ""),
        "email": settings.digest_recipient,
        "also_to": settings.also_to,
        "gmail_set": bool(env.get("GMAIL_APP_PASSWORD")),
        "ntfy_topic": env.get("NTFY_TOPIC", ""),
        "linkedin": browser.linkedin_signed_in(),
        "chrome_open": browser.cdp_endpoint() is not None,
        "google_available": google_auth.CLIENT_PATH.exists(),
        "google": google_auth.is_connected(),
        "google_error": _google_error.get("error", ""),
        "sheet_url": sheet.sheet_url(),
        "schedule": scheduler.status(),
    }


def _connect_google() -> None:
    _google_error.clear()
    try:
        google_auth.connect()
        sheet.sync(data_store.read_all() if data_store.CSV_PATH.exists() else [], {}, f"Job Hunter - {_profile().get('name', '')}")
    except Exception as exc:  # shown on the page, never fatal to the server
        _google_error["error"] = str(exc)


def schedule() -> dict:
    from src.setup_app import scheduler

    scheduler.install_jobs()
    wake = scheduler.install_wake()
    return {**scheduler.status(), "wake_set_now": wake}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode())

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/state":
            self._json(state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            if self.path == "/profile":
                write_env(
                    {
                        "GMAIL_APP_PASSWORD": body.get("gmail_app_password", "").replace(" ", ""),
                        "NTFY_TOPIC": body.get("ntfy_topic") or read_env().get("NTFY_TOPIC") or f"jobhunter-{secrets.token_hex(8)}",
                    }
                )
                self._json({"ok": True})
            elif self.path == "/login":
                if browser.cdp_endpoint() is None:
                    browser.open_for_login()
                self._json({"ok": True})
            elif self.path == "/google":
                threading.Thread(target=_connect_google, daemon=True).start()
                self._json({"ok": True})
            elif self.path == "/schedule":
                self._json({"ok": True, **schedule()})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            log.exception("setup action %s failed", self.path)
            self._json({"ok": False, "error": str(exc)}, 500)

    def log_message(self, *_args) -> None:
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    log.info("Job Hunter setup page: %s  (Ctrl+C to stop)", url)
    webbrowser.open(url)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
