"""One scheduled application run: newest postings first, until the run's target.

Searches LinkedIn Easy Apply jobs one keyword at a time (settings [search]
keywords, in order), first in the last 24 hours, then the last 7 and 30 days,
and stops once ``apply_attempts_per_run`` new applications have gone through.
Every filter in settings.toml still applies (titles, excluded companies, CTC),
and each cycle tries Lead / Senior CSM roles before the rest.

    cd ~/JobHunter/app && JOBHUNTER_HOME=~/JobHunter .venv/bin/python scripts/apply_run.py
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, run_poller  # noqa: E402
from src.common import data_store  # noqa: E402
from src.scrapers.plugins.linkedin import LinkedInScraper  # noqa: E402

log = logging.getLogger("apply_run")

WINDOWS_DAYS = (1, 7, 30, 90)  # newest first; older still-open postings only if short of the target (2026-10-15)
PAGES_PER_LOCATION = 10
RUN_MINUTES = 75  # searches stop after this so a run never blocks the next one (2026-10-19)
VET_LIMIT = 40   # company-site jobs checked per run


def _easy_apply_only() -> None:
    build = LinkedInScraper.build_search_url
    LinkedInScraper.build_search_url = staticmethod(lambda *a, **k: build(*a, **k) + "&f_AL=true")


def _applied() -> int:
    return sum(r["status"] == "applied" for r in data_store.read_all())


# ---------------------------------------------------------------------------
# LinkedIn Premium: "Jobs where you're a top applicant" (candidate, 2026-10-12)
# ---------------------------------------------------------------------------

TOP_APPLICANT_URL = "https://www.linkedin.com/jobs/collections/top-applicant/"
TOP_APPLICANT_PAGES = 10  # 25 per page; the list held 241 jobs on 2026-10-12


def _age_days(text: str) -> float:
    """Posting age from card text ("3 days ago", "2 weeks ago"); unknown sorts last."""
    import re

    found = re.search(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", text)
    if not found:
        return 999.0
    n, unit = int(found.group(1)), found.group(2)
    return n * {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}[unit]


def _role_rank(title: str) -> int:
    """Her order: Customer Success first, then Product Consultant, then Solutions."""
    t = title.lower()
    if "customer success" in t or "client success" in t:
        return 0
    if "product consultant" in t:
        return 1
    return 2


async def _scrape_top_applicant(ctx) -> list[dict]:
    """Jobs from the Premium top-applicant list (old ember layout: li[data-occludable-job-id])."""
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    jobs, seen_ids = [], set()
    page = await ctx.new_page()
    try:
        for n in range(TOP_APPLICANT_PAGES):
            await page.goto(f"{TOP_APPLICANT_URL}?start={n * 25}", wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(6)
            # The list renders lazily: scroll its pane so every card fills in.
            for _ in range(12):
                await page.evaluate("""() => { const l = document.querySelector('.scaffold-layout__list > div, .scaffold-layout__list');
                                               if (l) l.scrollBy(0, 500); }""")
                await asyncio.sleep(0.4)
            # Cards are blank until scrolled past ("occludable"): bring each one
            # into view and give it a moment to render before reading it.
            cards = await page.evaluate("""async () => {
                const out = [];
                for (const li of document.querySelectorAll('li[data-occludable-job-id]')) {
                    li.scrollIntoView({block: 'center'});
                    for (let i = 0; i < 10 && li.innerText.trim().split('\\n').length < 3; i++)
                        await new Promise(r => setTimeout(r, 150));
                    out.push({id: li.getAttribute('data-occludable-job-id'), text: li.innerText});
                }
                return out; }""")
            fresh = [c for c in cards if c["id"] and c["id"] not in seen_ids]
            if not fresh:
                break
            for c in fresh:
                seen_ids.add(c["id"])
                lines = [l.strip() for l in c["text"].splitlines() if l.strip() and l.strip() != "·"]
                if len(lines) < 3:
                    continue
                title = lines[0]
                rest = lines[2:] if lines[1].lower().startswith(title.lower()) else lines[1:]
                jobs.append({
                    "title": title, "company": rest[0], "location": rest[1] if len(rest) > 1 else "",
                    "platform": "linkedin", "url": f"https://www.linkedin.com/jobs/view/{c['id']}/",
                    "apply_type": "easy_apply" if "easy apply" in c["text"].lower() else "external_form",
                    "ctc": LinkedInScraper._salary_lpa(c["text"]), "yoe_required": None,
                    "posted_at": datetime.now(tz=ZoneInfo("Asia/Kolkata")), "top_applicant": True,
                    "age_days": _age_days(c["text"]),
                })
            log.info("top-applicant page %d: %d cards", n + 1, len(fresh))
    finally:
        await page.close()
    return jobs


async def _apply_top_applicant(base, budget: int) -> int:
    """Apply to the best top-applicant jobs that pass every filter. Returns applications made."""
    import json

    from src.apply import easy_apply, match
    from src.apply.question_bank import QuestionBank
    from src.common import home
    from src.common.browser import session

    async with session() as ctx:
        scraped = await _scrape_top_applicant(ctx)
        matched = match.filter_jobs([dict(j) for j in scraped], data_store.get_seen_urls(), base)
        # Company-site postings can't be auto-applied: list them in the tracker
        # (and so the sheet) as her to-do, best roles first.
        external = [j for j in matched if j["apply_type"] != "easy_apply"]
        external.sort(key=lambda j: (_role_rank(j["title"]), j.get("age_days", 999)))
        if external:
            data_store.append_jobs([{**j, "status": "scraped",
                                     "next_steps": "Top applicant (Premium) — apply on company site"} for j in external])
            log.info("top-applicant: %d company-site jobs added to the sheet to-do list", len(external))
        fresh = [j for j in matched if j["apply_type"] == "easy_apply"]
        fresh.sort(key=lambda j: (_role_rank(j["title"]), j.get("age_days", 999)))  # CS first, newest first
        log.info("top-applicant: %d listed, %d pass your filters (Easy Apply, Bangalore, roles)", len(scraped), len(fresh))
        if not fresh:
            return 0
        ids = data_store.append_jobs([{**j, "status": "scraped", "next_steps": "Top applicant (Premium)"} for j in fresh])
        profile = json.loads(home.form_profile_path().read_text(encoding="utf-8"))
        bank = QuestionBank(home.answers_path())
        history = data_store.read_all()
        applied_cos = {match.normalize_company(r["company"]) for r in history if r["status"] == "applied"}
        small_cos = {match.normalize_company(r["company"]) for r in history
                     if (r.get("next_steps") or "").startswith("Skipped: small company")}
        done = 0
        for job_id, job in zip(ids, fresh):
            if done >= budget:
                break
            key = match.normalize_company(job["company"])
            if key in applied_cos:
                data_store.update_job(job_id, next_steps="Skipped: already applied to this company")
                continue
            if key in small_cos:
                data_store.update_job(job_id, next_steps="Skipped: small company (seen before)")
                continue
            out = await easy_apply.apply(ctx, job, profile=profile, bank=bank,
                                         resume_keywords=base.resume_keywords, live=True, trace=None)
            bank.save()
            run_poller._record(job_id, out)
            if out.status == "applied":
                data_store.update_job(job_id, next_steps=f"Top applicant (Premium) — {out.reason}")
                applied_cos.add(key)
                done += 1
            log.info("top-applicant %s @ %s: %s — %s", job["title"], job["company"], out.status, out.reason)
        return done


def _blocked_ready(base) -> list[dict]:
    """Easy Apply jobs set aside earlier whose questions now all have answers.

    A run only searches for new postings, so without this a job that waited on
    a question was never tried again after the candidate answered it.
    """
    from src.apply.match import TitleRules, company_excluded, title_matches
    from src.apply.question_bank import QuestionBank
    from src.common import home

    answers = QuestionBank(home.answers_path())
    open_questions = {q for q, a in answers.entries().items() if a in (None, "")}
    from src.apply.match import normalize_company

    rules = TitleRules.from_settings(base)
    rows = data_store.read_all()
    applied_cos = {normalize_company(r["company"]) for r in rows if r["status"] == "applied"}
    ready = []
    for r in rows:
        note = r.get("next_steps") or ""
        if r["status"] != "manual_review" or r["apply_type"] != "easy_apply" or note.startswith("Skipped"):
            continue
        if normalize_company(r["company"]) in applied_cos:  # one application per company
            continue
        if company_excluded(r["company"], base.exclude_companies) or not title_matches(r["title"], base.titles, rules):
            continue
        if note.startswith("Needs your answer") and any(q in note for q in open_questions):
            continue
        if "no Easy Apply button" in note:
            continue
        ready.append(r)
    return ready


async def _retry(rows: list[dict], base, budget: int) -> None:
    import json

    from src.apply import easy_apply
    from src.apply.question_bank import QuestionBank
    from src.common import home
    from src.common.browser import session

    profile = json.loads(home.form_profile_path().read_text(encoding="utf-8"))
    bank = QuestionBank(home.answers_path())
    from src.apply.match import normalize_company

    done = 0
    applied_cos = {normalize_company(r["company"]) for r in data_store.read_all() if r["status"] == "applied"}
    async with session() as ctx:
        for r in rows:
            if done >= budget:
                return
            if normalize_company(r["company"]) in applied_cos:  # one application per company
                continue
            out = await easy_apply.apply(ctx, r, profile=profile, bank=bank,
                                         resume_keywords=base.resume_keywords, live=True, trace=None)
            bank.save()
            run_poller._record(r["id"], out)
            log.info("retry %s @ %s: %s — %s", r["title"], r["company"], out.status, out.reason)
            if out.status == "applied":
                done += 1
                applied_cos.add(normalize_company(r["company"]))


async def _vet_company_site_jobs() -> int:
    """Check company-site jobs found so far and keep only big, in-industry ones.

    She applies to these herself (or tells Claude how to) — the sheet should list
    only established companies in her industries (candidate, 2026-10-14).
    """
    import asyncio

    from src.apply.easy_apply import MIN_COMPANY_EMPLOYEES, _about_company, _company_size, _industry_ok
    from src.common.browser import session

    todo = [r for r in data_store.read_all()
            if r["status"] == "scraped" and r["apply_type"] != "easy_apply"
            and not (r.get("next_steps") or "").startswith(("Excluded", "Skipped", "Big company"))]
    todo = todo[-VET_LIMIT:]  # newest first-come; the rest are checked next run
    kept = 0
    async with session() as ctx:
        for r in todo:
            page = await ctx.new_page()
            try:
                await page.goto(r["url"], wait_until="domcontentloaded", timeout=45_000)
                await asyncio.sleep(4)
                body = await page.inner_text("body")
            except Exception:
                continue
            finally:
                await page.close()
            industry, size = _about_company(body)
            size = size or _company_size(body)
            top = "Top applicant (Premium) — " if "Top applicant" in (r.get("next_steps") or "") else ""
            if size is not None and size[1] < MIN_COMPANY_EMPLOYEES:
                data_store.update_job(r["id"], next_steps=f"Excluded: small company ({size[0]:,}-{size[1]:,} employees)")
            elif industry and not _industry_ok(industry):
                data_store.update_job(r["id"], next_steps=f"Excluded: industry not in your list ({industry})")
            else:
                band = f"{size[0]:,}+" if size and size[1] >= 10**9 else (f"{size[0]:,}-{size[1]:,}" if size else "size n/a")
                data_store.update_job(r["id"], next_steps=f"Big company — {top}apply on company site ({industry or 'industry n/a'}, {band} employees)")
                kept += 1
    log.info("company-site jobs vetted: %d checked, %d big in-industry kept for the sheet", len(todo), kept)
    return kept


def main() -> int:
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # Searches include company-site postings too (candidate, 2026-10-18): Easy Apply
    # ones are applied to, the rest are vetted and listed in the sheet for her.
    base = config.load()
    import os
    # Per-slot target from morning_run.sh (8:55 → 10, 19:00 → 10, 23:00 → 3).
    target = int(os.environ.get("APPLY_TARGET") or base.apply_attempts_per_run)
    start = _applied()

    # First, jobs that were only waiting on answers the candidate has since given
    # — Customer Success ones first (she is a CSM; 2026-10-12).
    ready = sorted(_blocked_ready(base), key=lambda r: "customer success" not in r["title"].lower())
    if ready:
        log.info("retrying %d job(s) whose questions are now answered", len(ready))
        try:
            asyncio.run(_retry(ready, base, target))
        except Exception:
            log.exception("retry pass failed — continuing with new searches")

    # Premium first: jobs where LinkedIn says she is a top applicant.
    got = _applied() - start
    if got < target:
        try:
            made = asyncio.run(_apply_top_applicant(base, target - got))
            log.info("top-applicant pass: %d new applications", made)
        except Exception:
            log.exception("top-applicant pass failed — continuing with searches")

    # Customer Success titles get every window (newest first) before any
    # Solutions Engineer / Product Consultant search starts (candidate, 2026-10-12).
    cs = [k for k in base.keywords if "customer success" in k.lower()]
    rest = [k for k in base.keywords if k not in cs]
    plan = [(days, k) for group in (cs, rest) for days in WINDOWS_DAYS for k in group]
    import time
    deadline = time.time() + RUN_MINUTES * 60  # wall clock: Mac sleep counts too
    for days, keyword in plan:
            if time.time() > deadline:
                log.info("time limit reached (%d min) — wrapping up", RUN_MINUTES)
                break
            got = _applied() - start
            if got >= target:
                log.info("target reached: %d new applications", got)
                break
            log.info("searching %r, last %d day(s) — %d/%d so far", keyword, days, got, target)
            settings = dataclasses.replace(
                base,
                keywords=[keyword],
                apply_attempts_per_run=target - got,
                window_seconds=days * 86_400,
                max_pages_per_location=PAGES_PER_LOCATION,
            )
            try:
                code = run_poller.run_cycle(settings, live=True, trace=False)
            except Exception:
                log.exception("cycle failed for %r — moving on", keyword)
                continue
            if code == 1:  # LinkedIn session dead or browser unavailable: stop, don't hammer it
                log.error("run_cycle reported a login/browser problem — stopping this run")
                return 1
    try:  # list big-company manual applications in the sheet
        asyncio.run(_vet_company_site_jobs())
    except Exception:
        log.exception("vetting company-site jobs failed")
    log.info("finished: %d new applications (target %d)", _applied() - start, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
