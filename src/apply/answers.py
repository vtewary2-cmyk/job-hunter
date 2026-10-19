"""Answer an Easy Apply question from the candidate's form profile.

Returns ``None`` whenever the profile does not actually hold the answer.  The
caller treats ``None`` on a required field as "abort this application": under
launchd there is nobody to ask, and a guessed answer on a real application is
worse than a skipped one.  In particular a question about years with a named
tool is answered only from ``skill_years`` — total experience is never
borrowed for a tool the candidate has not listed.

Money is stored once, in lakhs (``current_ctc_lpa``), and rendered in whatever
unit the question uses: "in Lakhs"/"LPA" → ``15``; a bare numeric field →
rupees, ``1500000``; free text → ``15 LPA``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

Kind = Literal["text", "numeric", "choice"]

_PLACEHOLDER = "[[NEED"
_NO_OPTION = "select an option"


def answer(label: str, kind: Kind, profile: dict, options: list[str] | None = None) -> str | None:
    """Return the value to enter, or None if the profile cannot answer."""
    norm = " ".join(label.lower().split())
    raw = _from_learned(norm, profile)
    if raw is None:
        raw = next((resolve(norm, kind, profile) for pattern, resolve in _RULES if pattern.search(norm)), None)
    if raw is None or _PLACEHOLDER in raw or not raw.strip():
        return None
    return pick_option(raw, options or []) if kind == "choice" else raw


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def _value(profile: dict, key: str) -> str | None:
    value = profile.get(key)
    return None if value is None else str(value)


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _from_learned(norm: str, profile: dict) -> str | None:
    mappings: dict = profile.get("learned_mappings", {})
    key = mappings.get(norm) or next((k for m, k in mappings.items() if m in norm), None)
    return _value(profile, key) if key else None


def _phone(_norm: str, _kind: Kind, profile: dict) -> str | None:
    digits = re.sub(r"\D", "", profile.get("phone", ""))
    # LinkedIn takes the country code in its own field; send the national number.
    return digits[2:] if len(digits) == 12 and digits.startswith("91") else digits or None


def _name_part(index: int) -> Callable[[str, Kind, dict], str | None]:
    def resolve(_norm: str, _kind: Kind, profile: dict) -> str | None:
        parts = profile.get("name", "").split()
        return parts[index] if parts else None

    return resolve


def _years(norm: str, _kind: Kind, profile: dict) -> str | None:
    after = norm.split("experience", 1)[-1]
    tool = re.search(r"\b(?:with|in|using)\s+(.+?)[\s?.]*$", after)
    if tool is None:
        return _value(profile, "years_experience")
    named = tool.group(1)
    skills: dict = profile.get("skill_years", {})
    return next((_number(years) for skill, years in skills.items() if skill.lower() in named), None)


def _notice(norm: str, kind: Kind, profile: dict) -> str | None:
    days = profile.get("notice_period_days")
    if days is None:
        return None
    # Plain number always: LinkedIn validates many "text" boxes as numeric and
    # rejected "30 days" with "Invalid input" (2026-10-09).
    return _number(days)


def _money(key: str) -> Callable[[str, Kind, dict], str | None]:
    def resolve(norm: str, kind: Kind, profile: dict) -> str | None:
        lpa = profile.get(key)
        if lpa is None:
            return None
        if re.search(r"lakh|lacs?\b|lpa|\bin l\b", norm):
            return _number(lpa)
        # Rupees otherwise, even in "text" boxes: LinkedIn rejected "15 LPA"
        # with "Invalid input" on several forms (2026-10-09).
        return _number(round(float(lpa) * 100_000))

    return resolve


def _authorized(_norm: str, _kind: Kind, profile: dict) -> str | None:
    return "Yes" if profile.get("work_authorization") else None


def _sponsorship(_norm: str, _kind: Kind, profile: dict) -> str | None:
    return "No" if "citizen" in profile.get("work_authorization", "").lower() else None


def _joins_in_time(norm: str, _kind: Kind, profile: dict) -> str | None:
    days = profile.get("notice_period_days")
    limit = re.search(r"within (\d+) days", norm)
    if days is None:
        return None
    return "Yes" if int(days) <= (int(limit.group(1)) if limit else 0) else "No"


def _range_ok(norm: str, _kind: Kind, profile: dict) -> str | None:
    """"Salary range is 15-20 LACS, ok?" — yes only if it reaches the expectation."""
    expected = profile.get("expected_ctc_lpa")
    band = re.search(r"(\d+)\s*-\s*(\d+)\s*(lacs|lakhs?|lpa)", norm)
    if expected is None or band is None:
        return None
    return "Yes" if float(band.group(2)) >= float(expected) else "No"


def _key(key: str) -> Callable[[str, Kind, dict], str | None]:
    return lambda _norm, _kind, profile: _value(profile, key)


# Ordered: the first matching pattern decides.  Salary rules sit above the
# years rule because "current CTC (years of experience based)" style labels
# exist, and the years rule must not swallow them.
_RULES: list[tuple[re.Pattern[str], Callable[[str, Kind, dict], str | None]]] = [
    (re.compile(r"country code"), lambda *_: "India (+91)"),
    (re.compile(r"e-?mail"), _key("email")),
    (re.compile(r"phone|mobile"), _phone),
    (re.compile(r"first name"), _name_part(0)),
    (re.compile(r"last name|surname"), _name_part(-1)),
    (re.compile(r"linkedin"), _key("linkedin_url")),
    (re.compile(r"github|portfolio|website"), _key("github_url")),
    (re.compile(r"(current|last drawn|present)\W+.*?(ctc|salary|compensation|annual|fixed|package)"), _money("current_ctc_lpa")),
    (re.compile(r"expected\W+.*?(ctc|salary|compensation|annual|package)"), _money("expected_ctc_lpa")),
    (re.compile(r"notice period|notice"), _notice),
    (re.compile(r"years?\b.*experience|experience.*\byears?"), _years),
    # Relocating to where the candidate already lives is a yes.
    (re.compile(r"relocat\w*.*\b(bangalore|bengaluru)\b|\b(bangalore|bengaluru)\b.*relocat"), lambda *_: "Yes"),
    (re.compile(r"relocat"), _key("willing_to_relocate")),
    (re.compile(r"sponsor|visa"), _sponsorship),
    (re.compile(r"authori[sz]ed to work|work authori[sz]ation|legally (allowed|eligible|able)|eligible to work"), _authorized),
    (re.compile(r"join within (\d+) days|immediate joiner"), _joins_in_time),
    (re.compile(r"salary range .*?(\d+)\s*-\s*(\d+)\s*(lacs|lakhs?|lpa)\b.*\b(ok|okay|fine|comfortable)"), _range_ok),
    (re.compile(r"^(current )?city\b|location \(city\)|current location"), _key("city")),
]


def pick_option(raw: str, options: list[str]) -> str | None:
    want = raw.strip().lower()
    real = [o for o in options if o.strip().lower() != _NO_OPTION]
    for test in (
        lambda o: o == want,
        lambda o: o.startswith(want),
        lambda o: want in o,
        lambda o: "+91" in o and want.startswith("india"),
    ):
        hit = next((o for o in real if test(o.strip().lower())), None)
        if hit is not None:
            return hit
    return None
