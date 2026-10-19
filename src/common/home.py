"""Where one candidate's files live on this machine.

Everything that differs between candidates — settings, form answers, resumes,
the tracker CSV, the signed-in Chrome profile — sits under one directory,
``$JOBHUNTER_HOME`` (default ``~/JobHunter``).  The code checkout holds none of
it, which is what lets the same code be zipped and handed to someone else.

Resolved on every call, not at import, so a test or the setup app can point
``JOBHUNTER_HOME`` somewhere else before asking.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

HOME_ENV: Final[str] = "JOBHUNTER_HOME"


def root() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "JobHunter"


def settings_path() -> Path:
    return root() / "settings.toml"


def env_path() -> Path:
    return root() / ".env"


def form_profile_path() -> Path:
    return root() / "form_profile.json"


def answers_path() -> Path:
    return root() / "answers.json"


def resumes_dir() -> Path:
    return root() / "resumes"


def data_dir() -> Path:
    return root() / "data"


def chrome_dir() -> Path:
    return root() / "chrome"
