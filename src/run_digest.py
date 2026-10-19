"""Daily digest — the slow half of the pipeline.

``run_daily.py`` used to scrape, filter, apply and notify in one 7 PM batch.
Scraping and applying moved to :mod:`src.run_poller`, which runs every 30
minutes because a once-daily run cannot reach a posting while it still has
single-digit applicant counts.  What is left here is the part that genuinely
belongs on a daily cadence: the summary email.

    python -m src.run_digest              # send it
    python -m src.run_digest --dry-run    # print the HTML instead
"""

from __future__ import annotations

import argparse
import logging
import sys

from src import config
from src.common import data_store, run_state
from src.notify.digest import build_digest, send_digest

log = logging.getLogger("digest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the daily job-hunt digest.")
    parser.add_argument("--dry-run", action="store_true", help="print the HTML, do not send")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    settings = config.load()

    data_store.init_csv()
    try:
        rows = data_store.read_all()
    except data_store.IntegrityError as exc:
        log.error("CSV integrity check failed: %s", exc)
        return 1

    skipped = run_state.load().get("skipped", {})
    html = build_digest(rows, skipped)

    if args.dry_run:
        sys.stdout.write(html)
        return 0

    if not settings.gmail_app_password:
        log.error("GMAIL_APP_PASSWORD is not set — cannot send. Use --dry-run to inspect.")
        return 1

    send_digest(html, settings)
    log.info("digest sent to %s (%d rows)", settings.digest_recipient, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
