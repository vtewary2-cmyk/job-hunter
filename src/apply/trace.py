"""A step-by-step record of what Easy Apply did, for a human or verifier to audit.

One run writes ``$JOBHUNTER_HOME/data/trace/<stamp>/trace.jsonl`` — one JSON
object per line — plus a screenshot of the form at every step in ``shots/``.
Each field line says what the form asked, what was entered, and where the
answer came from (``answers.json``, a profile ``rule``, LinkedIn's
``prefill``, or ``unanswered``), which is the evidence needed to say whether
an application was filled in correctly without having watched it happen.

Off unless ``--trace`` is passed: screenshots of every step of every form are
too heavy for the unattended daily run.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


class Trace:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "shots").mkdir(parents=True, exist_ok=True)
        self._file = root / "trace.jsonl"
        self._job = ""

    def event(self, kind: str, /, **data) -> None:
        line = {"ts": datetime.now(tz=_IST).isoformat(timespec="seconds"), "job": self._job, "event": kind, **data}
        with open(self._file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def start_job(self, job: dict) -> None:
        self._job = job.get("url", "")
        self.event("job_start", title=job.get("title", ""), company=job.get("company", ""))

    async def shot(self, target, label: str) -> str | None:
        """Screenshot a page or locator; never fails the application."""
        job_id = re.search(r"/view/(\d+)", self._job)
        name = f"{job_id.group(1) if job_id else 'job'}-{label}.png"
        try:
            await target.screenshot(path=str(self.root / "shots" / name))
            return f"shots/{name}"
        except Exception as exc:
            log.debug("trace screenshot failed: %s", exc)
            return None


class NoTrace(Trace):
    """The default: records nothing."""

    def __init__(self) -> None:  # noqa: D107 — deliberately no directory
        self._job = ""

    def event(self, kind: str, /, **data) -> None:
        return None

    async def shot(self, target, label: str) -> str | None:
        return None
