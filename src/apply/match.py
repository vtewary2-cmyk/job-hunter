"""Filter logic for the job-hunter apply module.

Applies 5 sequential gates to a list of scraped jobs and returns those that pass all.
Gate order: full-time → title → CTC/YOE → location → dedup.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Title gates
#
# The previous implementation stripped "senior"/"lead"/"staff"/"principal" as
# stop-words and then ran a Jaccard similarity over what was left.  Those words
# ARE the filter — removing them left `{engineer}`, and `Jaccard >= 0.5` then
# admitted "QA Engineer", "Sales Engineer" and anything else ending in
# "Engineer".  Three explicit gates are both stricter and easier to reason about.
# ---------------------------------------------------------------------------

# Disqualifying immediately, regardless of anything else.
_REJECT_RE = re.compile(
    r"\b("
    r"intern(ship)?|fresher|graduate|trainee|junior|jr\.?|apprentice|entry[\s-]?level"
    r"|qa|sdet|tester|test\s+engineer|automation\s+tester"
    r"|sales|recruit\w*|talent|support\s+engineer|customer\s+success"
    r"|engineering\s+manager|people\s+manager|director|vp\b|head\s+of|chief"
    r")\b",
    flags=re.IGNORECASE,
)

# Must look like an individual-contributor engineering role.
_CORE_RE = re.compile(
    r"\b(engineer|engineering|developer|sde|swe|programmer|architect)\b",
    flags=re.IGNORECASE,
)

# Must carry a senior signal.  Covers "SDE 2", "SDE-3", "SDE3", "Engineer II".
_SENIORITY_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|sde\s*-?\s*[23]|sde[23]|ii+|[23])\b",
    flags=re.IGNORECASE,
)



@dataclass(frozen=True)
class TitleRules:
    """The three title gates for one candidate's role family.

    The defaults above are an engineering filter — they reject "customer
    success" outright and require "engineer" — so any other role family must
    supply its own patterns via ``[filter] title_reject / title_core /
    title_seniority`` in settings.toml.  ``title_reject_extra`` adds words
    to whichever reject gate applies.
    """

    reject: re.Pattern[str]
    core: re.Pattern[str]
    seniority: re.Pattern[str]

    @classmethod
    def from_patterns(cls, reject: str, core: str, seniority: str) -> TitleRules:
        return cls(_compile_words(reject), _compile_words(core), _compile_words(seniority))

    @classmethod
    def from_settings(cls, settings) -> TitleRules:
        """Configured patterns, each falling back to the engineering default."""

        def configured(name: str, default: re.Pattern[str]) -> re.Pattern[str]:
            pattern = getattr(settings, name, None)
            return _compile_words(pattern) if pattern else default

        reject = configured("title_reject", _REJECT_RE)
        extra = getattr(settings, "title_reject_extra", None)
        if extra:
            # Extends the reject gate rather than replacing it, so a candidate
            # can add words without restating the defaults.
            reject = re.compile(rf"(?:{reject.pattern})|\b({extra})\b", flags=re.IGNORECASE)
        return cls(
            reject=reject,
            core=configured("title_core", _CORE_RE),
            seniority=configured("title_seniority", _SENIORITY_RE),
        )


def _compile_words(alternation: str) -> re.Pattern[str]:
    return re.compile(rf"\b({alternation})\b", flags=re.IGNORECASE)


ENGINEERING_TITLES = TitleRules(_REJECT_RE, _CORE_RE, _SENIORITY_RE)

# ---------------------------------------------------------------------------
# Keywords that indicate a non-full-time role
# ---------------------------------------------------------------------------
_NON_FULLTIME_PATTERNS: list[str] = [
    r"\bcontract(or)?\b",
    r"\bintern(ship)?\b",
    r"\bfreelance\b",
    r"\bpart[\s\-]?time\b",
    r"\btemporary\b",
    r"\btemp\b",
]
_NON_FULLTIME_RE = re.compile(
    "|".join(_NON_FULLTIME_PATTERNS),
    flags=re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Foreign-currency indicators — these make parse_ctc_lpa return None
# ---------------------------------------------------------------------------
_FOREIGN_CURRENCY_RE = re.compile(
    r"[\$£€]|SGD|AED|USD|GBP|EUR|CAD|AUD|HKD|JPY|CHF",
    flags=re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Allowed locations (normalised)
# ---------------------------------------------------------------------------
# Bangalore only, in the office or hybrid (candidate, 2026-10-12).
_ALLOWED_LOCATIONS: frozenset[str] = frozenset({"bengaluru", "bangalore"})


# ===========================================================================
# Public API
# ===========================================================================


def filter_jobs(
    jobs: list[dict],
    seen_urls: set[str],
    settings,
) -> list[dict]:
    """Apply all 5 gates sequentially.  Return jobs that pass every gate.

    Args:
        jobs:       Raw list of scraped job dicts.
        seen_urls:  Job URLs already recorded in the CSV.  Dedup is by URL, not
                    by company: overlapping poll windows re-surface the same
                    posting every run, and excluding a whole company because one
                    of its roles was once scraped silently starves the pipeline.
        settings:   A ``Settings`` dataclass with ``min_ctc_lpa``,
                    ``target_yoe``, and ``titles`` attributes.

    Returns:
        Subset of *jobs* that passed all 5 gates, preserving original order.
    """
    passed: list[dict] = []
    allowed_titles: list[str] = settings.titles
    target_ctc: float = settings.min_ctc_lpa
    target_yoe: int = settings.target_yoe
    title_rules = TitleRules.from_settings(settings)
    seen = {_canonical_url(u) for u in seen_urls}

    for job in jobs:
        title: str = job.get("title", "") or ""
        company: str = job.get("company", "") or ""
        ctc_str: str | None = job.get("ctc") or None
        yoe_str: str | None = job.get("yoe_required") or None
        location: str | None = job.get("location") or None

        # Gate 0 — never the candidate's current or past employers
        if company_excluded(company, getattr(settings, "exclude_companies", [])):
            logger.info("Gate 0 FAIL (excluded employer): %s @ %s", title, company)
            continue

        # Gate 1 — full-time check
        if not is_full_time(job):
            logger.debug("Gate 1 FAIL (not full-time): %s @ %s", title, company)
            continue

        # Gate 2 — title match
        if not title_matches(title, allowed_titles, title_rules):
            logger.debug("Gate 2 FAIL (title): %s @ %s", title, company)
            continue

        # Gate 3 — CTC / YOE
        if not ctc_matches(ctc_str, yoe_str, target_ctc, target_yoe):
            logger.debug("Gate 3 FAIL (ctc/yoe): %s @ %s", title, company)
            continue

        # Gate 4 — location
        if not location_matches(location, list(_ALLOWED_LOCATIONS)):
            logger.debug("Gate 4 FAIL (location=%s): %s @ %s", location, title, company)
            continue

        # Gate 5 — URL dedup
        if _canonical_url(job.get("url", "")) in seen:
            logger.debug("Gate 5 FAIL (already seen): %s @ %s", title, company)
            continue

        passed.append(job)

    logger.info("filter_jobs: %d/%d jobs passed all gates", len(passed), len(jobs))
    return passed


def is_full_time(job: dict) -> bool:
    """Return False if the title contains a non-full-time indicator.

    Checks the ``title`` field (and ``description`` if present) for keywords
    such as contract, intern, freelance, part-time, temporary / temp.
    """
    text = (job.get("title") or "") + " " + (job.get("description") or "")
    return not bool(_NON_FULLTIME_RE.search(text))


def _canonical_url(url: str) -> str:
    """Strip query/fragment and trailing slash so a URL is a stable dedup key.

    LinkedIn appends per-impression tracking params, so the raw href differs
    between polls for the same posting.
    """
    if not url:
        return ""
    return url.split("?")[0].split("#")[0].rstrip("/").lower()


def title_matches(
    title: str, allowed_titles: list[str], rules: TitleRules = ENGINEERING_TITLES
) -> bool:
    """Three gates, all of which must hold.

    1. No disqualifying token (default: intern, QA, sales, manager, director, …).
    2. Looks like the candidate's role family (default: engineer / developer / SDE …).
    3. Carries a seniority signal — OR matches a configured title verbatim,
       which lets ``settings.titles`` admit shapes the regex would miss.
    """
    if not title:
        return False
    t = title.lower()

    if rules.reject.search(t):
        return False
    if not rules.core.search(t):
        return False
    if rules.seniority.search(t):
        return True

    return any(a.lower() in t for a in allowed_titles if a)


def ctc_matches(
    ctc_str: str | None,
    yoe_str: str | None,
    target_ctc: float,
    target_yoe: int,
) -> bool:
    """Three-branch CTC / YOE gate.

    Branch 1: If ``parse_ctc_lpa(ctc_str)`` succeeds → pass iff the top of the
              band reaches ``target_ctc``.  This deliberately is NOT
              ``min <= target <= max``: that form rejected every posting paying
              MORE than the target ("45-60 LPA" against a 38 target failed on
              ``45 <= 38``), which threw away exactly the best jobs.
    Branch 2: Else if ``parse_yoe_range(yoe_str)`` succeeds → pass iff
              ``target_yoe`` is at or above the floor.  A 5-YOE candidate is a
              legitimate applicant to a "6-10 years" posting; the ceiling is
              aspirational, the floor is the real gate.  Allow one year of
              stretch below the floor.
    Branch 3: Else (neither parseable, foreign currency included) → True.
    """
    ctc_range = parse_ctc_lpa(ctc_str)
    if ctc_range is not None:
        _min_lpa, max_lpa = ctc_range
        return max_lpa >= target_ctc

    yoe_range = parse_yoe_range(yoe_str)
    if yoe_range is not None:
        min_yoe, max_yoe = yoe_range
        return (min_yoe - 1) <= target_yoe <= max_yoe

    # Neither parseable — keep
    return True


def parse_ctc_lpa(ctc_str: str | None) -> tuple[float, float] | None:
    """Parse an INR CTC string to an LPA range.

    Returns ``None`` for:
    - Foreign-currency strings (USD, GBP, SGD, EUR, AED, $, £, €, …)
    - Blank / None strings
    - Unparseable strings

    Handled formats:
    - "30-50 LPA"       → (30.0, 50.0)
    - "₹30L - ₹50L"    → (30.0, 50.0)
    - "30 to 50 LPA"   → (30.0, 50.0)
    - "30L"            → (30.0, 30.0)
    - "30,00,000"      → (30.0, 30.0)   (rupees ÷ 100_000)
    """
    if not ctc_str or not ctc_str.strip():
        return None

    s = ctc_str.strip()

    # Foreign-currency check — return None to "keep" the job
    if _FOREIGN_CURRENCY_RE.search(s):
        return None

    # Strip currency symbols and whitespace for easier parsing
    cleaned = re.sub(r"[₹,\s]", " ", s).strip()

    # Pattern: Indian comma-separated rupees (e.g. "30,00,000" after cleaning → "300000")
    # After cleaning commas we have a plain integer — detect by checking original had commas
    # and no "L"/"LPA" suffix
    original_no_space = re.sub(r"\s", "", s)
    if re.fullmatch(r"[\d,]+", original_no_space):
        # Pure number with commas → rupee amount
        rupees_str = original_no_space.replace(",", "")
        try:
            rupees = float(rupees_str)
            lpa = round(rupees / 100_000, 2)
            return (lpa, lpa)
        except ValueError:
            return None

    # Extract all numbers from the cleaned string
    # Supports: "30L", "30 L", "30 LPA", "30-50 LPA", "30 to 50 LPA"
    numbers = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if not numbers:
        return None

    try:
        values = [float(n) for n in numbers]
    except ValueError:
        return None

    if len(values) == 1:
        return (values[0], values[0])
    elif len(values) >= 2:
        # Take the first two numbers as min/max
        lo, hi = values[0], values[1]
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)

    return None


def parse_yoe_range(yoe_str: str | None) -> tuple[int, int] | None:
    """Parse a YOE string to an integer range.

    Supported formats:
    - "3-6 years"      → (3, 6)
    - "3+ years"       → (3, 99)
    - "minimum 3 yrs"  → (3, 99)
    - "3 to 6 years"   → (3, 6)
    - "3-7"            → (3, 7)

    Returns ``None`` for blank, None, or unparseable input.
    """
    if not yoe_str or not yoe_str.strip():
        return None

    s = yoe_str.strip().lower()

    # Pattern: "3+ years" or "3+"
    m = re.search(r"(\d+)\s*\+", s)
    if m:
        return (int(m.group(1)), 99)

    # Pattern: "minimum 3 yrs" / "min 3 years"
    m = re.search(r"min(?:imum)?\s+(\d+)", s)
    if m:
        return (int(m.group(1)), 99)

    # Pattern: "3-6 years" / "3 to 6 years" / "3-7" / "3 to 7"
    m = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)", s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)

    # Single number — treat as "exactly N years" → (N, N)
    m = re.search(r"(\d+)", s)
    if m:
        val = int(m.group(1))
        return (val, val)

    return None


def location_matches(location: str | None, allowed_locations: list[str]) -> bool:
    """Case-insensitive check whether *location* is an allowed city / remote.

    Allowed set: pune, bengaluru, bangalore, hyderabad, remote, work from home.
    Returns True if any allowed value appears as a substring of the (lowercased)
    location string.
    """
    if not location:
        return False
    loc_lower = location.lower().strip()
    # Bangalore on-site/hybrid/remote, or remote anywhere in India (candidate, 2026-10-15).
    if "remote" in loc_lower and ("india" in loc_lower or loc_lower.startswith("remote")
                                  or not any(c in loc_lower for c in ("usa", "united states", "uk", "europe", "singapore", "dubai", "uae"))):
        return True
    for allowed in allowed_locations:
        if allowed.lower() in loc_lower:
            return True
    return False


def company_excluded(company: str, excluded: list[str]) -> bool:
    """True if *company* names one of *excluded*, as whole words, any case.

    "Superset" matches "Superset (Joinsuperset)" but not "Supersetter";
    punctuation and spacing are ignored, so "Whitehat Jr" matches "WhiteHat Jr.".
    """
    words = " ".join(re.findall(r"[a-z0-9]+", (company or "").lower()))
    for name in excluded:
        needle = " ".join(re.findall(r"[a-z0-9]+", name.lower()))
        if needle and re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", words):
            return True
    return False


def normalize_company(name: str) -> str:
    """Lowercase and strip common corporate suffixes for dedup comparison.

    Suffixes removed: pvt, ltd, inc, private, limited, .com
    """
    if not name:
        return ""
    s = name.lower().strip()
    # Remove trailing punctuation + known suffixes iteratively
    suffixes = [
        r"\s+pvt\.?\s*ltd\.?$",
        r"\s+private\s+limited$",
        r"\s+pvt\.?$",
        r"\s+ltd\.?$",
        r"\s+limited$",
        r"\s+inc\.?$",
        r"\.com$",
    ]
    for pattern in suffixes:
        s = re.sub(pattern, "", s, flags=re.IGNORECASE).strip()
    return s
