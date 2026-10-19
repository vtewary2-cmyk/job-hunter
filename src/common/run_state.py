"""Per-agent heartbeat tracking and LLM spend accounting.

State is persisted as JSON in ``$JOBHUNTER_HOME/data/run_state.json``.  All writes are
atomic (write to a ``.tmp`` file, then ``os.replace()``).

Schema
------
::

    {
        "agents": {
            "<name>": {
                "last_success_ts": "",
                "last_task_summary": "",
                "last_status": ""
            }
        },
        "llm_spend": {"date": "YYYY-MM-DD", "usd": 0.0},
        "skipped": {
            "platforms": [
                {"platform": "", "reason": "", "ts": "", "retried": false}
            ],
            "jobs": [
                {
                    "id": "", "company": "", "title": "",
                    "url": "", "reason": "", "ts": "", "retried": false
                }
            ]
        }
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from src.common import home

DATA_DIR: Final[Path] = home.data_dir()
STATE_PATH: Final[Path] = DATA_DIR / "run_state.json"

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Default skeleton — used on first run or when the file is missing/corrupt.
# ---------------------------------------------------------------------------

_EMPTY_STATE: Final[dict] = {
    "agents": {},
    "llm_spend": {"date": "", "usd": 0.0},
    "skipped": {
        "platforms": [],
        "jobs": [],
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _now_ist_str() -> str:
    return _now_ist().isoformat()


def _today_ist_str() -> str:
    return _now_ist().date().isoformat()


def load() -> dict:
    """Load and return the run-state dict.

    Returns the empty skeleton if the file does not exist or cannot be
    parsed — this is safe because every function re-saves after mutating.
    """
    if not STATE_PATH.exists():
        return _deep_copy_empty()
    try:
        text = STATE_PATH.read_text(encoding="utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return _deep_copy_empty()


def save(state: dict) -> None:
    """Atomically write *state* to the state file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _deep_copy_empty() -> dict:
    """Return a fresh, deep-copied skeleton state dict."""
    import copy
    return copy.deepcopy(_EMPTY_STATE)


def _ensure_agent(state: dict, agent: str) -> None:
    state.setdefault("agents", {})
    state["agents"].setdefault(
        agent,
        {"last_success_ts": "", "last_task_summary": "", "last_status": ""},
    )


def _ensure_skipped(state: dict) -> None:
    state.setdefault("skipped", {"platforms": [], "jobs": []})
    state["skipped"].setdefault("platforms", [])
    state["skipped"].setdefault("jobs", [])


def _ensure_llm_spend(state: dict) -> None:
    state.setdefault("llm_spend", {"date": "", "usd": 0.0})


# ---------------------------------------------------------------------------
# Agent heartbeat
# ---------------------------------------------------------------------------


def record_agent_success(agent: str, summary: str) -> None:
    """Record a successful agent run with a human-readable *summary*."""
    state = load()
    _ensure_agent(state, agent)
    state["agents"][agent]["last_success_ts"] = _now_ist_str()
    state["agents"][agent]["last_task_summary"] = summary
    state["agents"][agent]["last_status"] = "success"
    save(state)


def record_agent_failure(agent: str, error: str) -> None:
    """Record a failed agent run, preserving the last success timestamp."""
    state = load()
    _ensure_agent(state, agent)
    # Do NOT overwrite last_success_ts — keep the last known good timestamp.
    state["agents"][agent]["last_task_summary"] = error
    state["agents"][agent]["last_status"] = "failure"
    save(state)


def did_run_today(agent: str, expected_hour_ist: int = 19) -> bool:
    """Return True if *agent* ran successfully today at or after *expected_hour_ist* IST.

    "Today" is determined by the current IST wall-clock date.

    Parameters
    ----------
    agent:
        Agent name key in the ``agents`` dict.
    expected_hour_ist:
        Minimum hour (0–23) of the last successful run for it to count as
        "today's run".  Default is 19 (7 PM IST, the normal daily schedule).
    """
    state = load()
    _ensure_agent(state, agent)
    ts_str = state["agents"][agent].get("last_success_ts", "")
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return False
    ts_ist = ts.astimezone(IST)
    today = _now_ist().date()
    return ts_ist.date() == today and ts_ist.hour >= expected_hour_ist


# ---------------------------------------------------------------------------
# LLM spend tracking
# ---------------------------------------------------------------------------


def get_llm_spend_today() -> float:
    """Return the USD amount spent on LLM calls today (IST date)."""
    state = load()
    _ensure_llm_spend(state)
    spend = state["llm_spend"]
    if spend.get("date", "") == _today_ist_str():
        return float(spend.get("usd", 0.0))
    return 0.0


def add_llm_spend(usd: float) -> None:
    """Accumulate *usd* into today's LLM spend total.

    If the stored date is not today, the spend is reset to *usd* first (same
    effect as calling :func:`reset_daily_spend_if_new_day` beforehand).
    """
    state = load()
    _ensure_llm_spend(state)
    today = _today_ist_str()
    if state["llm_spend"].get("date", "") != today:
        state["llm_spend"] = {"date": today, "usd": 0.0}
    state["llm_spend"]["usd"] = round(state["llm_spend"]["usd"] + usd, 6)
    save(state)


def reset_daily_spend_if_new_day() -> None:
    """Reset the LLM spend counter to 0 when the IST calendar date has changed."""
    state = load()
    _ensure_llm_spend(state)
    today = _today_ist_str()
    if state["llm_spend"].get("date", "") != today:
        state["llm_spend"] = {"date": today, "usd": 0.0}
        save(state)


# ---------------------------------------------------------------------------
# Skipped-platform tracking
# ---------------------------------------------------------------------------


def record_skipped_platform(platform: str, reason: str) -> None:
    """Append a skipped-platform entry to ``skipped.platforms``."""
    state = load()
    _ensure_skipped(state)
    state["skipped"]["platforms"].append(
        {
            "platform": platform,
            "reason": reason,
            "ts": _now_ist_str(),
            "retried": False,
        }
    )
    save(state)


def get_unretried_platforms() -> list[dict]:
    """Return all ``skipped.platforms`` entries where ``retried`` is false."""
    state = load()
    _ensure_skipped(state)
    return [p for p in state["skipped"]["platforms"] if not p.get("retried", False)]


def mark_retried_platform(platform: str) -> None:
    """Set ``retried=true`` for ALL ``skipped.platforms`` entries matching *platform*."""
    state = load()
    _ensure_skipped(state)
    for entry in state["skipped"]["platforms"]:
        if entry.get("platform") == platform:
            entry["retried"] = True
    save(state)


# ---------------------------------------------------------------------------
# Skipped-job tracking
# ---------------------------------------------------------------------------


def record_skipped_job(job: dict, reason: str) -> None:
    """Append a skipped-job entry to ``skipped.jobs``.

    *job* must contain ``id``, ``company``, ``title``, and ``url`` keys.
    """
    state = load()
    _ensure_skipped(state)
    state["skipped"]["jobs"].append(
        {
            "id": job.get("id", ""),
            "company": job.get("company", ""),
            "title": job.get("title", ""),
            "url": job.get("url", ""),
            "reason": reason,
            "ts": _now_ist_str(),
            "retried": False,
        }
    )
    save(state)


def get_unretried_jobs() -> list[dict]:
    """Return all ``skipped.jobs`` entries where ``retried`` is false."""
    state = load()
    _ensure_skipped(state)
    return [j for j in state["skipped"]["jobs"] if not j.get("retried", False)]


def mark_retried_job(job_id: str) -> None:
    """Set ``retried=true`` for the ``skipped.jobs`` entry matching *job_id*."""
    state = load()
    _ensure_skipped(state)
    for entry in state["skipped"]["jobs"]:
        if entry.get("id") == job_id:
            entry["retried"] = True
    save(state)
