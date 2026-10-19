"""Minutes-old job poller — the Strategy 1 entrypoint.

Run one cycle per invocation; launchd handles the cadence (see
``scripts/com.yash.jobhunter.poller.plist``).  Keeping the schedule outside the
process means a hung run cannot wedge the whole loop, and a laptop that slept
through a slot simply misses it instead of backing up.

    python -m src.run_poller --probe     # which selectors match right now
    python -m src.run_poller             # one cycle, dry-run (default)
    python -m src.run_poller --live      # same, but actually submits
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import filelock

from src import config
from src.apply import match
from src.apply import easy_apply
from src.apply.question_bank import QuestionBank
from src.apply.trace import Trace
from src.common import data_store, home
from src.common.browser import (
    BrowserUnavailable,
    is_logged_in_linkedin,
    open_for_login,
    session,
)
from src.notify import push
from src.scrapers.plugins.linkedin import LinkedInScraper

log = logging.getLogger("poller")

IST = ZoneInfo("Asia/Kolkata")

# One cycle at a time. Runs are 30 minutes apart, so an overlap means the
# previous one is stuck; the second should bow out rather than fight it for
# the browser.
_LOCK = data_store.DATA_DIR / "poller.lock"
_LOCK_TIMEOUT_S = 5

# Easy Apply attempts allowed per successful-application slot.
_ATTEMPTS_PER_SLOT = 3

# Titles the candidate wants applied to first (Lead CSM resume, senior level).
_LEAD_CSM = re.compile(
    r"(lead|senior|sr\.?|enterprise|principal|strategic|manager,?)\b.*(customer|client) success"
    r"|(customer|client) success.*\b(lead|senior manager)",
    re.IGNORECASE,
)


def _within_active_hours(settings) -> bool:
    hour = datetime.now(tz=IST).hour
    return settings.active_start_ist <= hour < settings.active_end_ist


_LOGIN_HINT = (
    "The job-hunter Chrome profile is not signed in to LinkedIn.\n"
    "Run `python -m src.run_poller --login`, sign in once in the window that\n"
    "opens, and leave it open. The session persists for every later run, and\n"
    "later runs attach to that window rather than opening another.\n\n"
    "Your everyday Chrome profile cannot be used for this: Chrome >= 136\n"
    "refuses automation on the default profile by design, to stop malware\n"
    "reading your cookies."
)


_PROBE_DIR = data_store.DATA_DIR / "probe"


async def _inmail_credits(context) -> dict:
    """Read the remaining InMail balance off the Premium page.

    Premium Career grants about five credits a month, and that number is the
    hard ceiling on the whole outreach feature — worth reading rather than
    assuming.
    """
    page = await context.new_page()
    try:
        await page.goto("https://www.linkedin.com/premium/my-premium/", timeout=45_000)
        await asyncio.sleep(3.0)
        body = await page.inner_text("body")
        return {
            "lines": [
                line.strip()
                for line in body.splitlines()
                if "inmail" in line.lower() or "credit" in line.lower()
            ][:15]
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        await page.close()


async def _probe(settings, open_modal: bool = False) -> int:
    async with session() as context:
        if not await is_logged_in_linkedin(context):
            print(f"\nNOT AUTHENTICATED.\n\n{_LOGIN_HINT}", file=sys.stderr)
            return 1

        scraper = LinkedInScraper()
        report = await scraper.probe(context, settings.keywords, settings.locations[0])

        if not any(n > 0 for n in report["selectors"]["card"].values()):
            print(json.dumps(report, indent=2))
            print("\nNo card selector matched — LinkedIn changed its DOM.", file=sys.stderr)
            return 1

        # One real posting, to answer the questions the search page cannot:
        # is the top-applicant signal visible before applying, is there a human
        # to message, and what does the Easy Apply form actually ask.
        jobs = await scraper.scrape_context(
            context,
            keywords=settings.keywords,
            locations=settings.locations[:1],
            window_seconds=settings.window_seconds,
            limit=1,
        )
        if jobs:
            report["detail"] = await scraper.probe_detail(
                context, jobs[0]["url"], _PROBE_DIR, open_apply_modal=open_modal
            )
            report["detail"]["job"] = {k: jobs[0][k] for k in ("title", "company", "apply_type")}
        else:
            report["detail"] = {"error": "no job in the window to open"}

        report["inmail"] = await _inmail_credits(context)
        print(json.dumps(report, indent=2))
    return 0


def _record(job_id: str, outcome: easy_apply.Outcome) -> None:
    """Write one apply outcome to the tracker. A dry run stays ``scraped``."""
    fields = {"next_steps": outcome.reason}
    if outcome.resume is not None:
        fields["resume"] = outcome.resume.name
    if outcome.status == "applied":
        fields.update(status="applied", applied_at=datetime.now(tz=IST).isoformat())
    elif outcome.status == "manual_review":
        fields["status"] = "manual_review"
    data_store.update_job(job_id, **fields)


async def _cycle(settings, live: bool, trace: Trace | None = None) -> int:
    if not _within_active_hours(settings):
        log.info(
            "outside active hours (%d-%d IST) — nothing to do",
            settings.active_start_ist,
            settings.active_end_ist,
        )
        return 0

    async with session() as context:
        if not await is_logged_in_linkedin(context):
            log.error("linkedin session is dead in the job-hunter profile")
            push.push(
                "Job hunter needs a login",
                "The LinkedIn session in the job-hunter Chrome profile expired.",
                click_url="https://www.linkedin.com/login",
                priority="high",
            )
            return 1

        scraper = LinkedInScraper()
        scraped = await scraper.scrape_context(
            context,
            keywords=settings.keywords,
            locations=settings.locations,
            window_seconds=settings.window_seconds,
            max_pages=settings.max_pages_per_location,
        )
        log.info("scraped %d listings from the last %ds", len(scraped), settings.window_seconds)
        if not scraped:
            return 0

        data_store.init_csv()
        seen = data_store.get_seen_urls()
        fresh = match.filter_jobs([dict(j) for j in scraped], seen, settings)
        # Lead / Senior Customer Success roles get the run's apply slots first;
        # Product Consultant and plain CSM roles follow (stable sort).
        fresh.sort(key=lambda j: not _LEAD_CSM.search(j.get("title") or ""))
        log.info("%d/%d passed the filter and are new", len(fresh), len(scraped))
        if not fresh:
            return 0

        ids = data_store.append_jobs([{**job, "status": "scraped"} for job in fresh])
        profile = json.loads(home.form_profile_path().read_text(encoding="utf-8"))
        bank = QuestionBank(home.answers_path())

        cap = settings.apply_attempts_per_run
        applied = attempts = 0
        # One application per company, ever (candidate, 2026-10-11).
        history = data_store.read_all()
        applied_cos = {match.normalize_company(r["company"]) for r in history if r["status"] == "applied"}
        # Companies already found too small: don't reopen their pages every run.
        small_cos = {
            match.normalize_company(r["company"]) for r in history
            if (r.get("next_steps") or "").startswith("Skipped: small company")
            and re.search(r"\((?:\d+)-(\d+) employees", r.get("next_steps") or "") and int(re.search(r"\((?:\d+)-(\d+) employees", r["next_steps"]).group(1)) < 201  # 200+ allowed (2026-10-15)
        }
        for job_id, job in zip(ids, fresh):
            company_key = match.normalize_company(job.get("company", ""))
            if company_key in applied_cos:
                data_store.update_job(job_id, next_steps="Skipped: already applied to this company")
                continue
            if company_key in small_cos:
                data_store.update_job(job_id, next_steps="Skipped: small company (seen before)")
                continue
            easy = job.get("apply_type") == "easy_apply"
            # Attempts are bounded too: a run where every form aborts must still
            # end, not walk every Easy Apply posting of the day.
            if not (easy and applied < cap and attempts < cap * _ATTEMPTS_PER_SLOT):
                # No push: external and over-cap jobs land in the Google Sheet,
                # which is the to-do list. A push per job was ~50 a day.
                continue
            attempts += 1
            outcome = await easy_apply.apply(
                context,
                job,
                profile=profile,
                bank=bank,
                resume_keywords=settings.resume_keywords,
                live=live,
                trace=trace,
            )
            bank.save()
            _record(job_id, outcome)
            log.info("%s @ %s: %s — %s", job["title"], job["company"], outcome.status, outcome.reason)
            if outcome.status == "manual_review":
                push.push_new_job(
                    job["title"], job["company"], job["url"], f"Auto-apply stopped: {outcome.reason}"
                )
            else:
                applied += 1
                if outcome.status == "applied":
                    applied_cos.add(company_key)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll LinkedIn for minutes-old postings.")
    parser.add_argument("--login", action="store_true", help="open the profile to sign in, then exit")
    parser.add_argument("--probe", action="store_true", help="report selector health and exit")
    parser.add_argument(
        "--probe-modal",
        action="store_true",
        help="with --probe, also open one Easy Apply modal to list its fields "
        "(never submits; discards the draft afterwards)",
    )
    parser.add_argument("--live", action="store_true", help="actually submit applications")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="record every form step (fields, answers, sources, screenshots) under data/trace/",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.login:
        open_for_login()
        print(
            "Opened the job-hunter Chrome profile with a debugging port.\n"
            "Sign in to LinkedIn there and LEAVE THE WINDOW OPEN — every later run\n"
            "attaches to it instead of launching a second browser."
        )
        return 0

    settings = config.load()

    if args.probe or args.probe_modal:
        return asyncio.run(_probe(settings, open_modal=args.probe_modal))

    return run_cycle(settings, live=args.live, trace=args.trace)


def run_cycle(settings, live: bool, trace: bool = False) -> int:
    """One scrape-filter-apply cycle under the profile lock."""
    data_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with filelock.FileLock(str(_LOCK), timeout=_LOCK_TIMEOUT_S):
            recorder = None
            if trace:
                recorder = Trace(data_store.DATA_DIR / "trace" / datetime.now(tz=IST).strftime("%Y%m%d-%H%M%S"))
                log.info("tracing to %s", recorder.root)
            return asyncio.run(_cycle(settings, live=live, trace=recorder))
    except filelock.Timeout:
        log.warning("a previous cycle is still running — skipping this slot")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
