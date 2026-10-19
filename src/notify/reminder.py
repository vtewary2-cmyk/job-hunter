"""The 8:55 AM heads-up before the 9:00 run.

Two channels, because neither is enough alone:

* **Phone push, booked a day ahead.**  Each run asks ntfy to hold a message
  until tomorrow 08:55 IST.  ntfy's server delivers it on the minute, whether
  or not the laptop is awake — which is exactly when a reminder matters.
* **Email at 08:55**, sent by a launchd job that the 08:53 wake schedule gets
  up for (``python -m src.notify.reminder``).  It reaches the candidate's inbox
  even without the ntfy app, but only if the laptop woke.

A booking is recorded in ``data/reminder.json`` so two runs on one day do not
book two pushes for the same morning.
"""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime, time, timedelta
from email.mime.text import MIMEText
from typing import Final
from zoneinfo import ZoneInfo

from src.common import home
from src.notify import push

log = logging.getLogger(__name__)

IST: Final = ZoneInfo("Asia/Kolkata")
REMINDER_AT: Final[time] = time(8, 55)

_TITLE: Final[str] = "Job Hunter runs at 9:00 AM"
_BODY: Final[str] = (
    "Today's job applications start at 9:00 AM. Keep the laptop open and plugged in, "
    "with the Job Hunter Chrome window open and signed in to LinkedIn."
)


def next_reminder(now: datetime | None = None) -> datetime:
    """The next 08:55 IST strictly after ``now``."""
    now = (now or datetime.now(tz=IST)).astimezone(IST)
    today = datetime.combine(now.date(), REMINDER_AT, tzinfo=IST)
    return today if now < today else today + timedelta(days=1)


def book_push(now: datetime | None = None) -> bool:
    """Book the next morning's push once.  Never raises."""
    at = next_reminder(now)
    state = home.data_dir() / "reminder.json"
    booked = json.loads(state.read_text()).get("booked_for") if state.exists() else None
    if booked == at.isoformat():
        return True
    if not push.push(_TITLE, _BODY, at=at):
        return False
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"booked_for": at.isoformat()}))
    log.info("reminder push booked for %s", at.isoformat())
    return True


def send_email(settings) -> bool:
    """Email the reminder to the candidate (and also_to).  Never raises."""
    if not settings.gmail_app_password:
        log.warning("no Gmail app password — reminder email not sent")
        return False
    recipients = [settings.digest_recipient, *settings.also_to]
    msg = MIMEText(_BODY + "\n", "plain", "utf-8")
    msg["Subject"] = _TITLE
    msg["From"] = f"Job Hunter <{settings.digest_recipient}>"
    msg["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(settings.digest_recipient, settings.gmail_app_password)
            server.sendmail(settings.digest_recipient, recipients, msg.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("reminder email failed: %s", exc)
        return False
    return True


def main() -> int:
    from src import config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    return 0 if send_email(config.load()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
