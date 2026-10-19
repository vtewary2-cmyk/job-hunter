"""Single writer for the job-applications CSV.

All external code must go through these functions — never write to the CSV
directly.  Every successful write recomputes the SHA-256 sidecar via
:mod:`src.common.integrity`.

CSV path  : $JOBHUNTER_HOME/data/job_applications.csv
Sidecar   : $JOBHUNTER_HOME/data/job_applications.csv.sha256
Lock file : $JOBHUNTER_HOME/data/job_applications.csv.lock
"""

from __future__ import annotations

import csv
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import filelock

from src.common import home
from src.common.integrity import IntegrityError, baseline, verify  # noqa: F401 (re-exported)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

DATA_DIR: Final[Path] = home.data_dir()

CSV_PATH: Final[Path] = DATA_DIR / "job_applications.csv"
SIDECAR_PATH: Final[Path] = DATA_DIR / "job_applications.csv.sha256"
LOCK_PATH: Final[Path] = DATA_DIR / "job_applications.csv.lock"

LOCK_TIMEOUT: Final[int] = 30  # seconds

HEADERS: Final[list[str]] = [
    "id",
    "title",
    "company",
    "platform",
    "url",
    "apply_type",
    "status",
    "ctc",
    "yoe_required",
    "location",
    "applied_at",
    "interview_date",
    "next_steps",
    "last_activity",
    # Which resume PDF was uploaded — a candidate with several variants needs
    # to know which one a recruiter is holding before the call.
    "resume",
    # When the pipeline first saw the posting. last_activity moves on every
    # update, so it cannot answer "when did this come in".
    "found_at",
]

VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "scraped",
        "applied",
        "manual_review",
        "interview_scheduled",
        "offer",
        "rejected",
    }
)

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

# Suffixes stripped when normalising company names for dedup.
_COMPANY_NOISE: Final[re.Pattern[str]] = re.compile(
    r"\b(pvt|ltd|inc|private|limited|\.com)\b",
    flags=re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_ist() -> str:
    """Return the current time as an ISO 8601 string with IST offset."""
    return datetime.now(tz=IST).isoformat()


def _validate_status(status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. Must be one of: {sorted(VALID_STATUSES)}"
        )


def _rows_to_dicts(rows: list[list[str]]) -> list[dict]:
    return [dict(zip(HEADERS, row)) for row in rows]


def _read_raw() -> list[list[str]]:
    """Read all data rows (no header) from the CSV without integrity check."""
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    # Skip header row if present.
    # A header from before a column was appended is a prefix of HEADERS; it is
    # still the header, not a data row.
    if rows and rows[0] == HEADERS[: len(rows[0])]:
        rows = rows[1:]
    # Rows written before a column was appended are short; pad so index-based
    # updates do not raise.
    return [row + [""] * (len(HEADERS) - len(row)) for row in rows]


def _write_all_rows(rows: list[list[str]]) -> None:
    """Overwrite the CSV with *rows* (no header in *rows*) then recompute sidecar."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADERS)
        writer.writerows(rows)
    baseline(CSV_PATH, SIDECAR_PATH)


def _normalise_company(name: str) -> str:
    """Lowercase + strip noisy suffixes for dedup comparison."""
    norm = name.lower().strip()
    norm = _COMPANY_NOISE.sub("", norm)
    # Collapse multiple spaces and strip again.
    return re.sub(r"\s+", " ", norm).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_csv() -> None:
    """Create the CSV file with the correct header row if it does not exist.

    Safe to call on every pipeline start — it is a no-op if the file is
    already present.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(HEADERS)
        baseline(CSV_PATH, SIDECAR_PATH)


def append_jobs(jobs: list[dict]) -> list[str]:
    """Append *jobs* to the CSV, assigning a UUID4 ``id`` to each.

    Returns the list of newly assigned IDs in the same order as *jobs*.

    Uses :class:`filelock.FileLock` for safe concurrent access.
    After writing, the SHA-256 sidecar is recomputed.
    """
    if not jobs:
        return []

    new_ids: list[str] = []
    now = _now_ist()

    lock = filelock.FileLock(str(LOCK_PATH), timeout=LOCK_TIMEOUT)
    with lock:
        existing_rows = _read_raw()
        new_rows: list[list[str]] = []

        for job in jobs:
            job_id = str(uuid.uuid4())
            new_ids.append(job_id)

            status = job.get("status", "scraped")
            _validate_status(status)

            row = [
                job_id,
                job.get("title", ""),
                job.get("company", ""),
                job.get("platform", ""),
                job.get("url", ""),
                job.get("apply_type", ""),
                status,
                job.get("ctc", ""),
                job.get("yoe_required", ""),
                job.get("location", ""),
                job.get("applied_at", ""),
                job.get("interview_date", ""),
                job.get("next_steps", ""),
                job.get("last_activity", now),
                job.get("resume", ""),
                job.get("found_at", now),
            ]
            new_rows.append(row)

        _write_all_rows(existing_rows + new_rows)

    return new_ids


def update_job(job_id: str, **fields) -> None:
    """Update the row whose ``id`` column matches *job_id*.

    Only the columns present in *fields* are modified; all others are left
    intact.  Raises :class:`KeyError` if *job_id* is not found.

    Uses :class:`filelock.FileLock` for safe concurrent access.
    After writing, the SHA-256 sidecar is recomputed.
    """
    if "status" in fields:
        _validate_status(fields["status"])

    lock = filelock.FileLock(str(LOCK_PATH), timeout=LOCK_TIMEOUT)
    with lock:
        rows = _read_raw()
        header_index = {col: i for i, col in enumerate(HEADERS)}

        matched = False
        for row in rows:
            if row[header_index["id"]] == job_id:
                matched = True
                for col, value in fields.items():
                    if col in header_index:
                        row[header_index[col]] = str(value)
                # Always bump last_activity on any update.
                if "last_activity" not in fields:
                    row[header_index["last_activity"]] = _now_ist()
                break

        if not matched:
            raise KeyError(f"Job ID not found: {job_id!r}")

        _write_all_rows(rows)


def read_all() -> list[dict]:
    """Read every row from the CSV as a list of dicts.

    Verifies SHA-256 integrity before reading.  Raises
    :class:`~src.common.integrity.IntegrityError` on hash mismatch.
    """
    verify(CSV_PATH, SIDECAR_PATH)
    rows = _read_raw()
    return _rows_to_dicts(rows)


def read_by_status(status: str) -> list[dict]:
    """Return rows whose ``status`` column equals *status*.

    Verifies SHA-256 integrity before reading.
    """
    _validate_status(status)
    verify(CSV_PATH, SIDECAR_PATH)
    rows = _read_raw()
    status_idx = HEADERS.index("status")
    filtered = [r for r in rows if r[status_idx] == status]
    return _rows_to_dicts(filtered)


def get_seen_urls() -> set[str]:
    """Return every job URL already recorded, canonicalised for comparison.

    This is the dedup key.  It replaced company-level dedup, which was wrong
    twice over: overlapping poll windows re-surface the same posting each run
    (so URL dedup is *required*), and excluding an entire company because one
    of its roles was once scraped permanently hid every future role there.
    """
    rows = _read_raw()
    url_idx = HEADERS.index("url")
    return {
        r[url_idx].split("?")[0].split("#")[0].rstrip("/").lower()
        for r in rows
        if len(r) > url_idx and r[url_idx]
    }


def get_companies() -> set[str]:
    """Normalised company names already in the CSV.

    No longer used for dedup (see :func:`get_seen_urls`) — retained because the
    digest reports per-company counts.
    """
    rows = _read_raw()
    company_idx = HEADERS.index("company")
    return {_normalise_company(r[company_idx]) for r in rows if r[company_idx]}
