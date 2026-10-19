"""Typed settings loader — reads $JOBHUNTER_HOME/settings.toml + environment variables."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.common import home

class ConfigError(Exception):
    pass


@dataclass
class Settings:
    # [general]
    enabled_plugins: list[str]

    # [search]
    keywords: list[str]
    locations: list[str]

    # [poll]
    interval_minutes: int
    window_seconds: int
    active_start_ist: int
    active_end_ist: int

    # [limits]
    apply_attempts_per_run: int
    max_pages_per_location: int

    # [filter]
    min_ctc_lpa: float
    target_yoe: int
    full_time_only: bool
    titles: list[str]
    # Title gate patterns (regex alternations); None keeps match.py's
    # engineering defaults, which reject every non-engineering role family.
    title_reject: str | None
    title_core: str | None
    title_seniority: str | None
    title_reject_extra: str | None
    # Companies never applied to (the candidate's current and past employers).
    exclude_companies: list[str]

    # [schedule]
    brief_poll_cron: str

    # [paths]
    resume_pdf: Path

    # [[resumes]] — PDF file name -> keywords that route a job to it
    resume_keywords: dict[str, list[str]]

    # [runtime]
    timezone: str

    # [notify]
    digest_recipient: str
    # Extra addresses copied on the digest and reminder; the sender stays
    # digest_recipient, whose Gmail app password signs in.
    also_to: list[str]
    smtp_host: str
    smtp_port: int

    # [funding]
    funding_enabled: bool
    funding_feeds: list[str]

    # From environment variables
    gmail_app_password: str
    anthropic_api_key: str
    anthropic_model: str
    daily_llm_usd_cap: float
    ntfy_topic: str


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Required environment variable '{name}' is not set. See .env.example.")
    return value


def _optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def _title_pattern(filter_cfg: dict, key: str) -> str | None:
    """Return a configured title regex, failing at load rather than mid-run."""
    pattern = filter_cfg.get(key)
    if pattern is None:
        return None
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"[filter] {key} is not a valid regex: {exc}") from exc
    return pattern


def _default_resume(configured: str) -> Path:
    """The resume used when no per-job pick applies (e.g. interview briefs).

    A configured name is resolved inside ``resumes/``; otherwise the first PDF
    there alphabetically, so a folder holding exactly one resume needs no config.
    """
    if configured:
        return home.resumes_dir() / configured
    pdfs = sorted(home.resumes_dir().glob("*.pdf"))
    return pdfs[0] if pdfs else home.resumes_dir() / "resume.pdf"


def load(toml_path: Path | None = None) -> Settings:
    """Load and validate settings from settings.toml + environment variables."""
    # Every entrypoint routes through here, so .env is read once, here. Only
    # run_brief_poller used to call load_dotenv(), which meant GMAIL_APP_PASSWORD
    # and NTFY_TOPIC in .env were silently invisible to the poller and digest.
    # Real environment wins over the file, so launchd's plist vars still take
    # precedence.
    from dotenv import load_dotenv

    load_dotenv(home.env_path())

    toml_path = toml_path or home.settings_path()
    if not toml_path.exists():
        raise ConfigError(
            f"Settings file not found: {toml_path}. Set {home.HOME_ENV} to the "
            f"candidate folder, or copy one from candidates/ to {home.root()}."
        )

    with open(toml_path, "rb") as f:
        cfg = tomllib.load(f)

    resume_pdf = _default_resume(cfg.get("paths", {}).get("resume_pdf", ""))

    poll = cfg.get("poll", {})
    funding = cfg.get("funding", {})

    return Settings(
        enabled_plugins=cfg["general"]["enabled_plugins"],
        keywords=cfg["search"]["keywords"],
        locations=cfg["search"]["locations"],
        interval_minutes=int(poll.get("interval_minutes", 30)),
        window_seconds=int(poll.get("window_seconds", 3600)),
        active_start_ist=int(poll.get("active_start_ist", 8)),
        active_end_ist=int(poll.get("active_end_ist", 22)),
        apply_attempts_per_run=int(cfg["limits"]["apply_attempts_per_run"]),
        max_pages_per_location=int(cfg["limits"].get("max_pages_per_location", 8)),
        min_ctc_lpa=float(cfg["filter"]["min_ctc_lpa"]),
        target_yoe=int(cfg["filter"]["target_yoe"]),
        full_time_only=bool(cfg["filter"]["full_time_only"]),
        titles=cfg["filter"]["titles"],
        title_reject=_title_pattern(cfg["filter"], "title_reject"),
        title_core=_title_pattern(cfg["filter"], "title_core"),
        title_seniority=_title_pattern(cfg["filter"], "title_seniority"),
        title_reject_extra=_title_pattern(cfg["filter"], "title_reject_extra"),
        exclude_companies=list(cfg["filter"].get("exclude_companies", [])),
        brief_poll_cron=cfg["schedule"]["brief_poll_cron"],
        resume_pdf=resume_pdf,
        resume_keywords={
            r["file"]: list(r.get("keywords", [])) for r in cfg.get("resumes", [])
        },
        timezone=cfg["runtime"]["timezone"],
        digest_recipient=cfg["notify"]["digest_recipient"],
        also_to=list(cfg["notify"].get("also_to", [])),
        smtp_host=cfg["notify"]["smtp_host"],
        smtp_port=int(cfg["notify"]["smtp_port"]),
        funding_enabled=bool(funding.get("enabled", False)),
        funding_feeds=list(funding.get("feeds", [])),
        # Secrets are optional at load time so `--probe` and `--dry-run` work on a
        # bare checkout. The code paths that actually need one check at use.
        gmail_app_password=_optional_env("GMAIL_APP_PASSWORD"),
        anthropic_api_key=_optional_env("ANTHROPIC_API_KEY"),
        anthropic_model=_optional_env("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        daily_llm_usd_cap=float(_optional_env("DAILY_LLM_USD_CAP", "1.00")),
        ntfy_topic=_optional_env("NTFY_TOPIC"),
    )
