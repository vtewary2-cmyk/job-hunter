#!/bin/bash
# Installs Job Hunter for the current macOS user and opens the setup page.
# Safe to run again: it updates the code in place and never overwrites the
# candidate's own settings, answers or data.
#
# Works from either source:
#   unzipped handoff bundle   "Start Job Hunter.command" -> app/scripts/bootstrap.sh
#   git clone of the repo     ./scripts/bootstrap.sh <candidate>   (or double-click
#                             "Start Job Hunter.command" at the repo root; it asks)
#
#   ~/JobHunter/            the candidate's files (settings, resumes, data, chrome)
#   ~/JobHunter/app/        the code and its Python, which launchd runs daily
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
JH_HOME="${JOBHUNTER_HOME:-$HOME/JobHunter}"
APP="$JH_HOME/app"
UV="$HOME/.local/bin/uv"
CHROME="/Applications/Google Chrome.app"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [ -d "$SCRIPTS/../candidates" ]; then
  # A git clone: code at the repo root, candidates/<name>/ for the files.
  REPO="$(cd "$SCRIPTS/.." && pwd)"
  NAME="${1:-}"
  if [ -z "$NAME" ]; then
    echo "Candidates in this repo: $(ls "$REPO/candidates" | tr '\n' ' ')"
    read -r -p "Set up which candidate? " NAME
  fi
  CAND="$REPO/candidates/$NAME"
  [ -d "$CAND" ] || { echo "No candidates/$NAME in $REPO"; exit 1; }
  copy_app() {
    mkdir -p "$APP/src" "$APP/scripts"
    rsync -a --delete --exclude "tests" --exclude "__pycache__" "$REPO/src/" "$APP/src/"
    cp "$REPO/requirements.txt" "$APP/"
    cp "$REPO/scripts/bootstrap.sh" "$APP/scripts/"
    if [ -f "$REPO/config/google_oauth_client.json" ]; then
      mkdir -p "$APP/config" && cp "$REPO/config/google_oauth_client.json" "$APP/config/"
    fi
  }
else
  # An unzipped bundle: this script is <bundle>/app/scripts/bootstrap.sh.
  BUNDLE="$(cd "$SCRIPTS/../.." && pwd)"
  CAND="$BUNDLE/candidate"
  copy_app() { rsync -a --delete --exclude ".venv" "$BUNDLE/app/" "$APP/"; }
fi

if [ ! -d "$CHROME" ]; then
  say "Google Chrome is needed. Opening the download page — install it, then double-click Start Job Hunter again."
  open "https://www.google.com/chrome/"
  exit 1
fi

say "1/4  Copying Job Hunter into $JH_HOME"
mkdir -p "$APP" "$JH_HOME/data" "$JH_HOME/resumes"
copy_app
# The candidate's starting files are copied only once: after setup they are
# theirs, edited by the setup page and by answering questions.
for f in settings.toml form_profile.json; do
  [ -f "$JH_HOME/$f" ] || cp "$CAND/$f" "$JH_HOME/$f"
done
cp -n "$CAND/resumes/"*.pdf "$JH_HOME/resumes/" 2>/dev/null || true

say "2/4  Getting the Python toolchain (first time only, about a minute)"
if [ ! -x "$UV" ] && ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
command -v uv >/dev/null 2>&1 && UV="$(command -v uv)"

say "3/4  Installing Job Hunter's Python packages"
"$UV" venv --quiet --allow-existing --python 3.11 "$APP/.venv"
"$UV" pip install --quiet --python "$APP/.venv/bin/python" -r "$APP/requirements.txt"

say "4/4  Opening the setup page — keep this window open while you set up"
cd "$APP"
if curl -s -o /dev/null "http://127.0.0.1:8765/state"; then
  open "http://127.0.0.1:8765/"
else
  JOBHUNTER_HOME="$JH_HOME" exec "$APP/.venv/bin/python" -m src.setup_app.server
fi
