"""Mirror the tracker into the candidate's own Google Sheet.

The first sync creates the spreadsheet in the signed-in person's Drive and
remembers its id in ``$JOBHUNTER_HOME/sheet.json``; every later sync writes to
that same sheet.  If the person deletes it, the next sync makes a new one.
Nothing has to be set up in Google beforehand — "Connect Google" on the setup
page (see :mod:`src.notify.google_auth`) is the only step.

Two tabs:

* **Jobs** — one row per posting, keyed by URL, with the date found and the
  date applied.  External postings read "Company site — apply manually": that
  column is the to-do list.  The first column is a checkbox, ticked and the
  row struck through once the job is applied.  Ticking it by hand after
  applying on a company site counts too: :func:`pull_ticked` reads it back so
  the tracker marks that job applied.
* **Questions** — every Easy Apply question and the answer given.  A blank
  answer is a question that stopped an application; typing it into the sheet
  is enough, because :func:`pull_answers` reads it back before the next run.

Failures are logged and swallowed: the sheet is a view, and a Google outage
must not stop applications.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Final

from src.common import home
from src.notify import google_auth

log = logging.getLogger(__name__)

_API: Final[str] = "https://sheets.googleapis.com/v4/spreadsheets"
_TIMEOUT_S: Final[float] = 60.0

JOB_COLUMNS: Final[list[str]] = [
    "Applied ✓", "Found", "Applied on", "Status", "Title", "Company", "Location",
    "How to apply", "Resume", "Notes", "URL",
]
_LAST_COL: Final[str] = chr(ord("A") + len(JOB_COLUMNS) - 1)
QUESTION_COLUMNS: Final[list[str]] = ["Question", "Answer"]
_URL_COL: Final[int] = JOB_COLUMNS.index("URL")

_HOW_TO_APPLY: Final[dict[str, str]] = {
    "easy_apply": "Easy Apply",
    "external_form": "Company site — apply manually",
}


class _SheetGone(Exception):
    """The remembered spreadsheet no longer exists (deleted or trashed)."""


def _date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def to_sheet_rows(rows: list[dict]) -> list[list]:
    """Tracker CSV rows -> Jobs-tab rows in JOB_COLUMNS order."""
    return [
        [
            r.get("status") == "applied",
            _date(r.get("found_at", "")),
            _date(r.get("applied_at", "")),
            r.get("status", ""),
            r.get("title", ""),
            r.get("company", ""),
            r.get("location", ""),
            _HOW_TO_APPLY.get(r.get("apply_type", ""), r.get("apply_type", "")),
            r.get("resume", ""),
            r.get("next_steps", ""),
            r.get("url", ""),
        ]
        for r in rows
    ]


def _api(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {google_auth.access_token()}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _SheetGone from exc
        raise


def _state_path():
    return home.root() / "sheet.json"


def sheet_url() -> str | None:
    path = _state_path()
    return json.loads(path.read_text())["url"] if path.exists() else None


def _create(title: str) -> str:
    made = _api(
        "POST",
        "",
        {
            "properties": {"title": title},
            "sheets": [
                {"properties": {"title": name, "gridProperties": {"frozenRowCount": 1}}}
                for name in ("Jobs", "Questions")
            ],
        },
    )
    sheet_id = made["spreadsheetId"]
    jobs_gid = made["sheets"][0]["properties"]["sheetId"]
    _format_jobs(sheet_id, jobs_gid)
    _api(
        "POST",
        f"/{sheet_id}/values:batchUpdate",
        {
            "valueInputOption": "RAW",
            "data": [
                {"range": "Jobs!A1", "values": [JOB_COLUMNS]},
                {"range": "Questions!A1", "values": [QUESTION_COLUMNS]},
            ],
        },
    )
    _state_path().write_text(json.dumps({"id": sheet_id, "url": made["spreadsheetUrl"]}))
    log.info("created the tracker sheet: %s", made["spreadsheetUrl"])
    return sheet_id


def _format_jobs(sheet_id: str, gid: int) -> None:
    """Checkbox in column A; an applied row is struck through and greyed out."""
    rows_below_header = {"sheetId": gid, "startRowIndex": 1}
    _api(
        "POST",
        f"/{sheet_id}:batchUpdate",
        {
            "requests": [
                {
                    "setDataValidation": {
                        "range": {**rows_below_header, "startColumnIndex": 0, "endColumnIndex": 1},
                        "rule": {"condition": {"type": "BOOLEAN"}},
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "index": 0,
                        "rule": {
                            "ranges": [rows_below_header],
                            "booleanRule": {
                                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=$A2=TRUE"}]},
                                "format": {
                                    "textFormat": {"strikethrough": True},
                                    "backgroundColor": {"red": 0.9, "green": 0.96, "blue": 0.9},
                                },
                            },
                        },
                    }
                },
            ]
        },
    )


def _sheet_id(title: str) -> str:
    path = _state_path()
    return json.loads(path.read_text())["id"] if path.exists() else _create(title)


def _values(sheet_id: str, a1: str) -> list[list[str]]:
    return _api("GET", f"/{sheet_id}/values/{urllib.parse.quote(a1)}").get("values", [])


def _append(sheet_id: str, tab: str, rows: list[list[str]]) -> None:
    if rows:
        query = "valueInputOption=RAW&insertDataOption=INSERT_ROWS"
        _api("POST", f"/{sheet_id}/values/{tab}!A1:append?{query}", {"values": rows})


def _ticked(cell) -> bool:
    return str(cell).strip().upper() == "TRUE"


def _write(sheet_id: str, rows: list[list], questions: dict[str, str | None]) -> int:
    # Jobs: overwrite rows whose URL is already there, append the rest. A
    # tick put in by hand is kept even if the tracker has not caught up yet.
    current = _values(sheet_id, f"Jobs!A2:{_LAST_COL}")
    existing = {r[_URL_COL]: (i + 2, _ticked(r[0])) for i, r in enumerate(current) if len(r) > _URL_COL}
    updates, appends = [], []
    for row in rows:
        at, ticked = existing.get(row[_URL_COL], (None, False))
        if at:
            row = [row[0] or ticked, *row[1:]]
            updates.append({"range": f"Jobs!A{at}:{_LAST_COL}{at}", "values": [row]})
        else:
            appends.append(row)
            existing[row[_URL_COL]] = (-1, False)
    if updates:
        _api("POST", f"/{sheet_id}/values:batchUpdate", {"valueInputOption": "RAW", "data": updates})
    _append(sheet_id, "Jobs", appends)

    # Questions: add new ones; fill a blank answer the laptop knows; never
    # blank an answer typed into the sheet.
    seen = {
        r[0].strip().lower(): (i + 2, r[1] if len(r) > 1 else "")
        for i, r in enumerate(_values(sheet_id, "Questions!A2:B"))
        if r
    }
    fills, new = [], []
    for question, answer in questions.items():
        key = question.strip().lower()
        if key not in seen:
            new.append([question, answer or ""])
            seen[key] = (-1, answer or "")
        elif seen[key][0] > 0 and not seen[key][1] and answer:
            fills.append({"range": f"Questions!B{seen[key][0]}", "values": [[answer]]})
    if fills:
        _api("POST", f"/{sheet_id}/values:batchUpdate", {"valueInputOption": "RAW", "data": fills})
    _append(sheet_id, "Questions", new)
    return len(rows)


def sync(rows: list[dict], questions: dict[str, str | None], title: str) -> bool:
    """Upsert every job and question into the candidate's sheet.  Never raises."""
    try:
        sheet_id = _sheet_id(title)
        try:
            count = _write(sheet_id, to_sheet_rows(rows), questions)
        except _SheetGone:
            log.warning("the tracker sheet was deleted — creating a new one")
            _state_path().unlink(missing_ok=True)
            count = _write(_create(title), to_sheet_rows(rows), questions)
    except google_auth.GoogleAuthError as exc:
        log.warning("google sheet skipped: %s", exc)
        return False
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        log.warning("google sheet sync failed: %s", exc)
        return False
    log.info("google sheet: %d jobs synced", count)
    return True


def pull_answers() -> dict[str, str]:
    """Answers typed into the Questions tab, blank ones left out.  Never raises."""
    path = _state_path()
    if not path.exists():
        return {}
    try:
        rows = _values(json.loads(path.read_text())["id"], "Questions!A2:B")
    except (google_auth.GoogleAuthError, _SheetGone, urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("could not read answers from the sheet: %s", exc)
        return {}
    return {r[0]: r[1].strip() for r in rows if len(r) > 1 and r[1].strip()}


def pull_ticked() -> set[str]:
    """URLs whose Applied box is ticked in the sheet.  Never raises."""
    path = _state_path()
    if not path.exists():
        return set()
    try:
        rows = _values(json.loads(path.read_text())["id"], f"Jobs!A2:{_LAST_COL}")
    except (google_auth.GoogleAuthError, _SheetGone, urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("could not read ticks from the sheet: %s", exc)
        return set()
    return {r[_URL_COL] for r in rows if len(r) > _URL_COL and _ticked(r[0])}
