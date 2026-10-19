"""Phone push notifications via ntfy.sh.

The poller runs unattended while the user is away from the desk, which is
exactly when a macOS notification is useless.  ntfy delivers to the phone,
needs no account, and its ``Click`` header makes the notification open the job
URL directly.

Set ``NTFY_TOPIC`` to a long random string — anyone who knows the topic can
read your notifications.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Final

log = logging.getLogger(__name__)

_BASE: Final[str] = "https://ntfy.sh"
_TIMEOUT_S: Final[float] = 10.0


def _topic() -> str | None:
    return os.environ.get("NTFY_TOPIC", "").strip() or None


def push(
    title: str,
    body: str,
    click_url: str | None = None,
    priority: str = "default",
    at: datetime | None = None,
) -> bool:
    """Send one notification — now, or held by ntfy until ``at``.  True on success.

    ``at`` uses ntfy's scheduled delivery: the server holds the message and
    delivers it on time even while this laptop is asleep or shut.

    Never raises: a failed notification must not take down a scrape run.
    """
    topic = _topic()
    if not topic:
        log.warning("NTFY_TOPIC unset — dropping notification: %s", title)
        return False

    # ntfy carries metadata in headers, which must be latin-1 clean. Titles
    # routinely contain em dashes and the odd emoji, so transliterate rather
    # than let urllib raise mid-notification.
    headers = {
        "Title": title.encode("ascii", "replace").decode("ascii"),
        "Priority": priority,
    }
    if click_url:
        headers["Click"] = click_url
    if at is not None:
        headers["At"] = str(int(at.timestamp()))

    req = urllib.request.Request(
        f"{_BASE}/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("ntfy push failed (%s): %s", title, exc)
        return False


def push_new_job(title: str, company: str, url: str, reason: str) -> bool:
    """Notify about a job the pipeline could not auto-apply to."""
    return push(
        title=f"⚡ {title} @ {company}",
        body=f"{reason}\nApply now — this posting is minutes old.",
        click_url=url,
        priority="high",
    )
