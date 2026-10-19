"""Print {"manual": rows, "applied": rows} for the sheet's two simple tabs.

"Apply manually": good big-company jobs she applies to herself — top applicant
first, Customer Success first, one row per company+role, with a Done ✓ box.
"Applied": every application made for her, newest first.

    cd ~/JobHunter/app && JOBHUNTER_HOME=~/JobHunter .venv/bin/python scripts/sheet_tabs.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.common import data_store  # noqa: E402


def _rank(title: str) -> int:
    t = title.lower()
    return 0 if ("customer success" in t or "client success" in t) else 1 if "product consultant" in t else 2


def main() -> int:
    rows = data_store.read_all()
    manual = [r for r in rows if (r.get("next_steps") or "").startswith("Big company")]
    manual.sort(key=lambda r: ("Top applicant" not in r["next_steps"], _rank(r["title"]), r["company"].lower()))
    seen, man = set(), [["Done ✓", "Top applicant?", "Company", "Role", "Location", "Industry & size", "Found", "Apply link"]]
    for r in manual:
        key = (r["company"].lower(), r["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        m = re.search(r"\(([^()]*employees)\)", r["next_steps"])
        man.append([False, "Yes" if "Top applicant" in r["next_steps"] else "", r["company"], r["title"], r["location"],
                    m.group(1) if m else "", (r.get("found_at") or "")[:10], r["url"]])
    applied = sorted([r for r in rows if r["status"] == "applied"], key=lambda r: r["applied_at"], reverse=True)
    app = [["Applied on", "Company", "Role", "Location", "Resume used", "Link"]]
    for r in applied:
        app.append([r["applied_at"][:16].replace("T", " "), r["company"], r["title"], r["location"],
                    (r.get("resume") or "").replace("Vanshika_Tewary_Resume_", "").replace(".pdf", ""), r["url"]])
    print(json.dumps({"manual": man, "applied": app}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
