"""Referrals, top-down: top-applicant company-site jobs -> her 1st/2nd-degree contacts there.

Starts from LinkedIn Premium's "top applicant" list (not from her network), so
it runs only a couple of company-filtered people searches per company and never
sweeps her connections — that sweep is what risks LinkedIn restrictions.

  * Company-site (non-Easy-Apply) jobs only, and only ones that pass settings.toml
    (roles, location, pay, excluded companies) and the 200+ employee / industry gate.
  * Skips companies she has already applied to (one application per company).
  * Superset people (current or former) are never listed.
  * Drafts messages only; nothing is sent.

    cd ~/JobHunter/app && JOBHUNTER_HOME=~/JobHunter .venv/bin/python scripts/ref_top.py [max_companies]
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from apply_run import _role_rank, _scrape_top_applicant  # noqa: E402
from src import config  # noqa: E402
from src.apply import match  # noqa: E402
from src.apply.easy_apply import MIN_COMPANY_EMPLOYEES, _about_company, _company_size, _industry_ok  # noqa: E402
from src.common import data_store  # noqa: E402
from src.common.browser import session  # noqa: E402

DATA = Path.home() / "JobHunter/data"
OUT = DATA / "ref_top.json"
MAX_COMPANIES = int(sys.argv[1]) if len(sys.argv) > 1 else 5
PAUSE_S = (20, 40)  # between LinkedIn requests, so the searches look like a person's
CONTACTS_PER_CO = 3
_RELEVANT = re.compile(r"customer success|client success|solution|account|recruit|talent|hiring|\bhr\b|people", re.I)


async def _pause() -> None:
    await asyncio.sleep(random.uniform(*PAUSE_S))


async def _company(ctx, job_url: str) -> dict:
    """Company id, size and industry from the job page (company id via the company page)."""
    pg = await ctx.new_page()
    try:
        await pg.goto(job_url, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(5)
        body = await pg.inner_text("body")
        industry, size = _about_company(body)
        size = size or _company_size(body)
        href = await pg.evaluate("""() => { const a = [...document.querySelectorAll('a[href*="/company/"]')]
                                              .find(a => !/\\/company\\/(setup|jobs)/.test(a.href)); return a ? a.href : null; }""")
        cid = None
        if href:
            await _pause()
            await pg.goto(href.split("?")[0], wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(5)
            html = await pg.content()
            m = (re.search(r"urn:li:fsd_company:(\d+)", html) or re.search(r"urn:li:organization:(\d+)", html)
                 or re.search(r'"companyId"\s*:\s*(\d+)', html) or re.search(r"currentCompany=%5B%22(\d+)", html))
            cid = m.group(1) if m else None
        return {"company_id": cid, "industry": industry, "size": size}
    finally:
        await pg.close()


async def _people(ctx, cid: str, degree: str) -> list[dict]:
    """One results page of people currently at the company, for one degree ("F" or "S")."""
    q = urllib.parse.urlencode({"currentCompany": f'["{cid}"]', "network": f'["{degree}"]', "origin": "FACETED_SEARCH"})
    pg = await ctx.new_page()
    try:
        await pg.goto(f"https://www.linkedin.com/search/results/people/?{q}", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(5)
        for _ in range(4):
            await pg.mouse.wheel(0, 900)
            await asyncio.sleep(0.5)
        found = await pg.evaluate("""() => {
            const res = [];
            for (const a of document.querySelectorAll('a[href*="/in/"]')) {
                const t = a.innerText || '';
                const d = t.match(/•\\s*(1st|2nd|3rd)/); if (!d) continue;
                const m = a.href.match(/linkedin\\.com\\/in\\/([^/?#]+)/); if (!m) continue;
                // Headline sits in the card around the name link.
                const card = a.closest('li, [data-view-name], div[componentkey]') || a.parentElement;
                res.push({slug: m[1], degree: d[1], name: t.split('\\n')[0], card: (card ? card.innerText : t)});
            }
            return res; }""")
    finally:
        await pg.close()
    out, seen = [], set()
    for f in found:
        if f["slug"] in seen:
            continue
        seen.add(f["slug"])
        name = re.sub(r"\s*(View .*|•.*)$", "", f["name"]).strip()
        lines = [l.strip() for l in f["card"].splitlines() if l.strip()]
        headline = next((l for l in lines if l != name and not re.search(
            r"^(• )?(1st|2nd|3rd)|degree|mutual|^Message$|^Follow$|^Connect$|^View |^Current:|^Past:", l)), "")
        out.append({"name": name, "headline": headline, "degree": f["degree"],
                    "url": f"https://www.linkedin.com/in/{f['slug']}/"})
    return out


def _draft(c: dict, job: dict) -> str:
    first = c["name"].split()[0] if c["name"] else "there"
    if c["degree"] == "1st":  # a message, under 100 words
        return (f"Hi {first}, hope you're doing well! I'm applying for the {job['title']} role at {job['company']} "
                f"({job['url']}) and LinkedIn lists me as a top applicant. I have 5+ years in B2B SaaS customer "
                f"success, owning onboarding, renewals and expansion for enterprise accounts. Would you be open to "
                f"referring me, or pointing me to the hiring manager? Happy to share my resume. Thank you!")
    # 2nd degree: a connection note (LinkedIn caps these at 300 characters)
    return (f"Hi {first}, I'm applying for the {job['title']} role at {job['company']} and I'm a top applicant on "
            f"LinkedIn. 5+ yrs in B2B SaaS customer success. Would love to connect and ask about a referral. Thanks!")


async def main() -> None:
    base = config.load()
    superset = {p["url"] for p in json.load(open(DATA / "superset_connections.json"))}
    history = data_store.read_all()
    applied_cos = {match.normalize_company(r["company"]) for r in history if r["status"] == "applied"}
    done_before = {r["company"] for r in (json.load(open(OUT)) if OUT.exists() else [])}

    async with session() as ctx:
        scraped = await _scrape_top_applicant(ctx)
        matched = match.filter_jobs([dict(j) for j in scraped], set(), base)
        jobs = [j for j in matched if j["apply_type"] != "easy_apply"]
        jobs.sort(key=lambda j: (_role_rank(j["title"]), j.get("age_days", 999)))
        by_co: dict[str, dict] = {}
        for j in jobs:
            key = match.normalize_company(j["company"])
            if key in applied_cos or j["company"] in done_before or key in by_co:
                continue
            by_co[key] = j  # best role per company
        print(f"top applicant: {len(scraped)} listed, {len(jobs)} company-site jobs pass your filters, "
              f"{len(by_co)} new companies; checking {min(MAX_COMPANIES, len(by_co))}", flush=True)

        results = json.load(open(OUT)) if OUT.exists() else []
        for job in list(by_co.values())[:MAX_COMPANIES]:
            co = await _company(ctx, job["url"])
            row = {"company": job["company"], "job": {k: job[k] for k in ("title", "location", "url")},
                   "industry": co["industry"], "company_id": co["company_id"], "contacts": [], "excluded_superset": 0}
            size = co["size"]
            if size is not None and size[1] < MIN_COMPANY_EMPLOYEES:
                row["skipped"] = f"small company ({size[0]}-{size[1]})"
            elif co["industry"] and not _industry_ok(co["industry"]):
                row["skipped"] = f"industry not in your list ({co['industry']})"
            elif not co["company_id"]:
                row["skipped"] = "company id not found"
            else:
                found = []
                for degree in ("F", "S"):
                    await _pause()
                    found += await _people(ctx, co["company_id"], degree)
                keep = [p for p in found if p["url"] not in superset and "superset" not in p["headline"].lower()]
                row["excluded_superset"] = len(found) - len(keep)
                keep.sort(key=lambda p: (p["degree"] != "1st", not _RELEVANT.search(p["headline"])))
                row["contacts"] = [{**p, "draft": _draft(p, job)} for p in keep[:CONTACTS_PER_CO]]
                row["found"] = {"1st": sum(p["degree"] == "1st" for p in keep), "2nd": sum(p["degree"] == "2nd" for p in keep)}
            results.append(row)
            json.dump(results, open(OUT, "w"), indent=1, ensure_ascii=False)
            print(f"{job['company']} — {job['title']}: {row.get('skipped') or row['found']}", flush=True)
            await _pause()


if __name__ == "__main__":
    asyncio.run(main())
