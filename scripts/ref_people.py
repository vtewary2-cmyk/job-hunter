import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
"""People-search helper: 1st-degree connections for a LinkedIn people-search filter."""
import asyncio, re, json, sys, urllib.parse
from src.common.browser import session

async def people(ctx, params: dict, max_pages: int = 10) -> list[dict]:
    out, seen = [], set()
    pg = await ctx.new_page()
    try:
        for n in range(1, max_pages + 1):
            q = urllib.parse.urlencode({**params, "network": '["F"]', "page": n})
            await pg.goto(f"https://www.linkedin.com/search/results/people/?{q}", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)
            for _ in range(4):
                await pg.mouse.wheel(0, 900); await asyncio.sleep(0.4)
            found = await pg.evaluate("""() => {
                const res = [];
                for (const a of document.querySelectorAll('a[href*="/in/"]')) {
                    const t = a.innerText || '';
                    if (!/•\\s*(1st|2nd|3rd)/.test(t)) continue;  // main result card, not a mutual-connection link
                    const m = a.href.match(/linkedin\\.com\\/in\\/([^/?#]+)/); if (!m) continue;
                    const lines = t.split('\\n').map(s => s.trim()).filter(s => s && !/^•/.test(s));
                    res.push({slug: m[1], lines: lines.slice(0, 4)});
                }
                return res; }""")
            new = 0
            for f in found:
                if f["slug"] in seen or not f["lines"] or "mutual connection" in f["lines"][0]: continue
                seen.add(f["slug"]); new += 1
                name = re.sub(r"\s*View .*$", "", f["lines"][0]).strip()
                headline = next((l for l in f["lines"][1:] if not re.search(r"^(• )?1st|degree|connection|Message|Follow|Connect", l)), "")
                out.append({"name": name, "headline": headline, "url": f"https://www.linkedin.com/in/{f['slug']}/"})
            if new == 0: break
    finally:
        await pg.close()
    return out

if __name__ == "__main__":
    async def main():
        async with session() as ctx:
            cur = await people(ctx, {"currentCompany": '["10658796"]'})
            past = await people(ctx, {"pastCompany": '["10658796"]'})
        ex = {p["url"]: p for p in cur + past}
        json.dump(list(ex.values()), open(sys.argv[1], "w"), indent=1, ensure_ascii=False)
        print("current Superset connections:", len(cur), "| former:", len(past), "| excluded total:", len(ex))
        for p in list(ex.values())[:5]: print("  e.g.", p["name"], "—", p["headline"][:60])
    asyncio.run(main())
