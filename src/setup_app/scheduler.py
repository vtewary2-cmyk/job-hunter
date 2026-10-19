"""Install the daily schedule on this Mac.

Three pieces, all per-user except the wake:

    08:53  pmset wakes the Mac          (needs the admin password, asked once)
    08:55  launchd: reminder email       com.jobhunter.reminder
    09:00  launchd: the daily run        com.jobhunter.daily

launchd, not cron: a Mac that slept through 09:00 runs the job when it wakes,
where cron would silently skip the day.  The wake is ``pmset repeat``, which
only takes effect with the lid open or on power with a display attached — the
reminder tells the candidate exactly that.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Final

from src.common import home

log = logging.getLogger(__name__)

CODE_DIR: Final[Path] = Path(__file__).parents[2]
AGENTS_DIR: Final[Path] = Path.home() / "Library" / "LaunchAgents"
WAKE_AT: Final[str] = "08:53:00"

JOBS: Final[dict[str, tuple[str, int, int]]] = {
    # label: (module, hour, minute)
    "com.jobhunter.reminder": ("src.notify.reminder", 8, 55),
    "com.jobhunter.daily": ("src.run_daily", 9, 0),
}


def plist_for(label: str, module: str, hour: int, minute: int, python: str = sys.executable) -> dict:
    log_path = str(home.data_dir() / f"{label.rsplit('.', 1)[-1]}.log")
    return {
        "Label": label,
        "ProgramArguments": [python, "-m", module],
        "WorkingDirectory": str(CODE_DIR),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        # launchd agents do not read the shell profile; everything the run
        # needs must be here or in $JOBHUNTER_HOME/.env.
        "EnvironmentVariables": {
            home.HOME_ENV: str(home.root()),
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install_jobs(run=subprocess.run) -> None:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    home.data_dir().mkdir(parents=True, exist_ok=True)
    for label, (module, hour, minute) in JOBS.items():
        path = AGENTS_DIR / f"{label}.plist"
        path.write_bytes(plistlib.dumps(plist_for(label, module, hour, minute)))
        run(["launchctl", "bootout", f"{_domain()}/{label}"], capture_output=True)
        result = run(["launchctl", "bootstrap", _domain(), str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"launchctl could not load {label}: {result.stderr.strip()}")
        log.info("installed %s at %02d:%02d", label, hour, minute)


def install_wake(run=subprocess.run) -> bool:
    """Ask for the admin password once (macOS dialog) and set the daily wake."""
    script = f'do shell script "pmset repeat wakeorpoweron MTWRFSU {WAKE_AT}" with administrator privileges'
    result = run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("wake schedule not set: %s", result.stderr.strip())
    return result.returncode == 0


def status(run=subprocess.run) -> dict:
    jobs = {
        label: run(["launchctl", "print", f"{_domain()}/{label}"], capture_output=True).returncode == 0
        for label in JOBS
    }
    sched = run(["pmset", "-g", "sched"], capture_output=True, text=True).stdout
    return {"jobs": jobs, "wake": "wakeorpoweron" in sched.lower() or "wake or power on" in sched.lower()}
