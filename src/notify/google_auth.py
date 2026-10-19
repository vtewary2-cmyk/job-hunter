"""Sign in to Google once, from the laptop, with no Google library.

Installed-app OAuth (RFC 8252): the setup page opens Google's consent screen
in the person's normal browser, Google redirects back to a one-shot server on
``127.0.0.1``, and the code is exchanged — with PKCE — for a refresh token
stored in ``$JOBHUNTER_HOME/google_token.json`` (mode 600).  Every later run
trades that refresh token for a short-lived access token.

The only scope is ``drive.file``: access to files *this app created* and
nothing else in the person's Drive.  It is the reason the consent screen needs
no Google verification, and the reason the app can create the tracker sheet
but could never read anything else.

The OAuth client (``config/google_oauth_client.json``) is a Desktop client.
Google documents that a Desktop client's secret cannot be kept confidential,
so it ships inside the handoff zip; what protects the account is the consent
click and the token that never leaves the laptop.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Final

from src.common import home

log = logging.getLogger(__name__)

SCOPE: Final[str] = "https://www.googleapis.com/auth/drive.file"
_AUTH_URL: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL: Final[str] = "https://oauth2.googleapis.com/token"
CLIENT_PATH: Final[Path] = Path(__file__).parents[2] / "config" / "google_oauth_client.json"
_CONSENT_TIMEOUT_S: Final[int] = 300


class GoogleAuthError(RuntimeError):
    """No usable Google sign-in: not connected yet, revoked, or misconfigured."""


def _client() -> dict:
    if not CLIENT_PATH.exists():
        raise GoogleAuthError(f"OAuth client missing: {CLIENT_PATH}")
    raw = json.loads(CLIENT_PATH.read_text(encoding="utf-8"))
    return raw.get("installed", raw)


def token_path() -> Path:
    return home.root() / "google_token.json"


def is_connected() -> bool:
    return token_path().exists()


def _post(url: str, form: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise GoogleAuthError(f"Google token endpoint said {exc.code}: {exc.read()[:300]!r}") from exc


def _save(token: dict) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(token), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class _Catch(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catch.result = {k: v[0] for k, v in query.items()}
        ok = "code" in _Catch.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = "Google is connected. You can close this tab." if ok else "Google sign-in was cancelled."
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{message}</h2></body></html>".encode())

    def log_message(self, *_args) -> None:
        return None


def connect(open_browser=webbrowser.open) -> None:
    """Run the consent flow and store the refresh token.  Blocks until done."""
    client = _client()
    server = http.server.HTTPServer(("127.0.0.1", 0), _Catch)
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = _AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    _Catch.result = {}
    server.timeout = _CONSENT_TIMEOUT_S
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    open_browser(url)
    thread.join(_CONSENT_TIMEOUT_S)
    server.server_close()

    result = _Catch.result
    if result.get("state") != state or "code" not in result:
        raise GoogleAuthError(f"Google sign-in did not complete: {result.get('error', 'no response')}")
    token = _post(
        _TOKEN_URL,
        {
            "code": result["code"],
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if "refresh_token" not in token:
        raise GoogleAuthError("Google returned no refresh token; connect again.")
    _save({"refresh_token": token["refresh_token"]})
    log.info("google connected; token saved to %s", token_path())


_cache: dict = {}


def access_token() -> str:
    """A valid access token, refreshed when within a minute of expiry."""
    if _cache.get("expires", 0) - 60 > time.time():
        return _cache["access_token"]
    if not is_connected():
        raise GoogleAuthError("Google is not connected — use Connect Google on the setup page.")
    client = _client()
    refresh = json.loads(token_path().read_text(encoding="utf-8"))["refresh_token"]
    token = _post(
        _TOKEN_URL,
        {
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )
    _cache.update(access_token=token["access_token"], expires=time.time() + int(token.get("expires_in", 3600)))
    return _cache["access_token"]
