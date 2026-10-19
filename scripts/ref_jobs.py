"""Jobs at companies where she has 1st-degree connections (LinkedIn "In my network")."""
import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncio, json
from src import config
from src.apply import match
from src.common.browser import session
from src.scrapers.plugins.linkedin import LinkedInScraper as L

_build = L.build_search_url
L.build_search_url = staticmethod(lambda *a, **k: _build(*a, **k) + "&f_JIYN=true")  # In my network

async def main(out: str) -> None:
    s = config.load()
    jobs, seen = [], set()
    async with session() as ctx:
        for kw in s.keywords:  # one title at a time; Customer Success titles come first in settings
            for j in await L().scrape_context(ctx, keywords=[kw], locations=s.locations, window_seconds=30 * 86400, max_pages=3):
                if j["url"] not in seen:
                    seen.add(j["url"]); jobs.append(j)
    ok = match.filter_jobs([dict(j) for j in jobs], set(), s)  # keep jobs already seen: still worth a referral
    for j in ok:
        j["posted_at"] = str(j.get("posted_at"))
    json.dump(ok, open(out, "w"), indent=1, ensure_ascii=False)
    print(f"in-network jobs found: {len(jobs)} | matching your rules: {len(ok)} | companies: {len({j['company'] for j in ok})}")

asyncio.run(main(sys.argv[1]))
