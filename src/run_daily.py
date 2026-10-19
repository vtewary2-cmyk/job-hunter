"""The one job launchd runs at 9:00 AM.

    python -m src.run_daily             # live: submits up to the apply cap
    python -m src.run_daily --dry-run   # walks every form, submits nothing

Steps run in order and each one's failure is logged and survived, so a Google
outage never stops applications and a LinkedIn problem never stops the digest:

    sheet answers + ticks -> answers.json / tracker
    scrape -> filter -> Easy Apply          (run_poller.run_cycle)
    tracker + questions -> Google Sheet
    digest email
    book tomorrow's 08:55 reminder push
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime

from src import config, run_poller
from src.apply.question_bank import QuestionBank
from src.common import data_store, home, run_state
from src.notify import reminder, sheet
from src.notify.digest import build_digest, send_digest

log = logging.getLogger("daily")


def _sheet_title() -> str:
    try:
        name = json.loads(home.form_profile_path().read_text(encoding="utf-8")).get("name", "")
    except (OSError, ValueError):
        name = ""
    return f"Job Hunter - {name}".strip(" -")


def pull_from_sheet(bank: QuestionBank) -> None:
    """Bring back what the candidate typed or ticked in the Sheet."""
    for question, answer in sheet.pull_answers().items():
        bank.record(question, answer)
    bank.save()
    ticked = sheet.pull_ticked()
    if not ticked:
        return
    data_store.init_csv()
    for row in data_store.read_all():
        if row["url"] in ticked and row["status"] != "applied":
            data_store.update_job(
                row["id"],
                status="applied",
                applied_at=datetime.now(tz=reminder.IST).isoformat(),
                next_steps="Marked applied in the Sheet",
            )
            log.info("marked applied from the Sheet: %s @ %s", row["title"], row["company"])


def _step(name: str, fn) -> None:
    try:
        fn()
    except Exception:  # each step is independent; see module docstring
        log.exception("daily step '%s' failed — continuing", name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily job-hunter run.")
    parser.add_argument("--dry-run", action="store_true", help="walk forms but submit nothing")
    parser.add_argument("--trace", action="store_true", help="record every form step")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    settings = config.load()
    bank = QuestionBank(home.answers_path())
    _step("pull from sheet", lambda: pull_from_sheet(bank))

    code = 1
    try:
        code = run_poller.run_cycle(settings, live=not args.dry_run, trace=args.trace)
    except Exception:
        log.exception("apply cycle failed — continuing to the sheet and digest")

    rows: list[dict] = []
    _step("read tracker", lambda: rows.extend(data_store.read_all()))
    questions = QuestionBank(home.answers_path()).entries()
    _step("sync sheet", lambda: sheet.sync(rows, questions, _sheet_title()))
    if settings.gmail_app_password:
        _step("digest", lambda: send_digest(build_digest(rows, run_state.load().get("skipped", {})), settings))
    else:
        log.warning("no Gmail app password — digest not sent")
    _step("book reminder", reminder.book_push)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
