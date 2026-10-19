"""Print the Google Sheet contents as JSON: {"jobs": [[...]], "questions": [[...]]}.

Used by the Claude scheduled task that mirrors the tracker into the sheet with
the Google Sheets connector (the app's own Google sign-in is not available).
Both lists include their header row. Account Manager rows are marked excluded,
since the candidate never applies to them.

    cd ~/JobHunter/app && JOBHUNTER_HOME=~/JobHunter .venv/bin/python scripts/sheet_rows.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.common import data_store, home  # noqa: E402
from src.apply.question_bank import QuestionBank  # noqa: E402
from src.notify import sheet  # noqa: E402


def _job_rows(rows: list[dict]) -> list[list]:
    to_rows = next(
        f for f in vars(sheet).values()
        if callable(f) and "Tracker CSV rows" in (getattr(f, "__doc__", "") or "")
    )
    jobs = to_rows(rows)
    for row in jobs:
        if (row[9] or "").startswith("Excluded"):
            row[3], row[7] = "excluded", "Not applying"
        elif re.search(r"account manag", row[4], re.I):
            row[3], row[7], row[9] = "excluded", "Not applying", "Excluded: Account Manager role"
    return jobs


def main() -> int:
    rows = data_store.read_all()
    answers = QuestionBank(home.answers_path()).entries()
    out = {
        "jobs": [sheet.JOB_COLUMNS] + _job_rows(rows),
        "questions": [sheet.QUESTION_COLUMNS] + [[q, a if a is not None else ""] for q, a in answers.items()],
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
