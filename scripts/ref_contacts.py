"""For each in-network job, list her 1st-degree connections at that company (Superset people excluded)."""
import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncio, json, re
from src.common.browser import session
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ref_people import people

async def company_id(ctx, job_url: str) -> str | None:
    pg = await ctx.new_page()
    try:
        await pg.goto(job_url, wait_until="domcontentloaded", timeout=60000); await asyncio.sleep(5)
        html = await pg.content()
        m = re.search(r"currentCompany=%5B%22(\d+)%22%5D", html) or re.search(r'currentCompany=\["(\d+)"\]', html) \
            or re.search(r"urn:li:fsd_company:(\d+)", html) or re.search(r'"companyId":(\d+)', html)
        return m.group(1) if m else None
    finally:
        await pg.close()

async def main(jobs_file: str, out: str) -> None:
    jobs = json.load(open(jobs_file))
    superset = {p["url"] for p in json.load(open(str(Path.home() / "JobHunter/data/superset_connections.json")))}
    by_co: dict[str, dict] = {}
    for j in jobs:
        by_co.setdefault(j["company"], {"company": j["company"], "jobs": [], "url": j["url"]})["jobs"].append(
            {"title": j["title"], "location": j["location"], "url": j["url"], "apply_type": j["apply_type"]})
    async with session() as ctx:
        for co in by_co.values():
            cid = await company_id(ctx, co["url"])
            co["company_id"] = cid
            found = await people(ctx, {"currentCompany": f'["{cid}"]'}, max_pages=3) if cid else []
            co["contacts"] = [p for p in found if p["url"] not in superset and "superset" not in p["headline"].lower()]
            co["excluded_superset"] = len(found) - len(co["contacts"])
            print(f"{co['company']}: {len(co['contacts'])} contact(s){' (+'+str(co['excluded_superset'])+' Superset excluded)' if co['excluded_superset'] else ''}", flush=True)
    json.dump(list(by_co.values()), open(out, "w"), indent=1, ensure_ascii=False)

asyncio.run(main(sys.argv[1], sys.argv[2]))
