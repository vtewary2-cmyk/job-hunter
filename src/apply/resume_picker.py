"""Pick which resume to upload for a given job.

The variants are whatever PDFs sit in ``$JOBHUNTER_HOME/resumes/``.  Each can
carry a keyword list in ``settings.toml``::

    [[resumes]]
    file = "Vanshika_Tewary_LeadCSM.pdf"
    keywords = ["customer success", "renewals", "churn"]

Scoring is a keyword count over title + description, with a title hit worth
double: the title is what the hiring manager wrote first, the description is
mostly boilerplate.  The highest score wins; a tie (including nobody scoring)
goes to the variant listed first in settings, then to unconfigured PDFs in
name order.  A folder holding exactly one PDF therefore needs no config at all.

A model call per job would be slower, cost money on every run, and be no more
accurate at choosing between two or three known documents.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Final

from src.common import home

log = logging.getLogger(__name__)

_TITLE_WEIGHT: Final[int] = 2
_DESCRIPTION_WEIGHT: Final[int] = 1


class ResumeNotBuilt(RuntimeError):
    """No resume PDF exists to upload."""


def _variants(resume_keywords: dict[str, list[str]], resumes_dir: Path) -> list[Path]:
    """Existing PDFs, configured ones first in settings order."""
    configured = [resumes_dir / name for name in resume_keywords if (resumes_dir / name).exists()]
    others = sorted(p for p in resumes_dir.glob("*.pdf") if p not in configured)
    return configured + others


def _hits(keywords: list[str], text: str) -> int:
    return sum(
        1 for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", text, flags=re.IGNORECASE)
    )


def score_variants(
    title: str,
    description: str = "",
    *,
    resume_keywords: dict[str, list[str]],
    resumes_dir: Path | None = None,
) -> dict[Path, int]:
    """Return the score of every existing variant against this job text."""
    resumes_dir = resumes_dir or home.resumes_dir()
    return {
        pdf: _TITLE_WEIGHT * _hits(resume_keywords.get(pdf.name, []), title)
        + _DESCRIPTION_WEIGHT * _hits(resume_keywords.get(pdf.name, []), description)
        for pdf in _variants(resume_keywords, resumes_dir)
    }


def pick(
    title: str,
    description: str = "",
    *,
    resume_keywords: dict[str, list[str]],
    resumes_dir: Path | None = None,
) -> Path:
    """Return the PDF to upload for this job, raising if there is none."""
    resumes_dir = resumes_dir or home.resumes_dir()
    scores = score_variants(
        title, description, resume_keywords=resume_keywords, resumes_dir=resumes_dir
    )
    if not scores:
        raise ResumeNotBuilt(f"No resume PDF in {resumes_dir} — add one before applying.")
    # max() keeps the first of equal scores, and the dict is in priority order.
    chosen = max(scores, key=scores.__getitem__)
    log.debug("resume for %r: %s (scores %s)", title, chosen.name, {p.name: s for p, s in scores.items()})
    return chosen
