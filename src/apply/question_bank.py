"""Every Easy Apply question seen, with the answer given — or null.

``$JOBHUNTER_HOME/answers.json`` maps question text to answer::

    {
      "Notice period (in days)": "30",
      "How many years of experience do you have with TypeScript?": null
    }

A ``null`` is a question the profile could not answer: that application was
aborted, and filling the value in makes every later form asking the same
question go through.  An answer, once in the file, is the answer — it beats the
built-in rules in ``answers.py`` and is never overwritten by them, so editing
this file is how the candidate corrects anything the pipeline got wrong.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _key(question: str) -> str:
    return " ".join(question.lower().split())


class QuestionBank:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, str | None] = self._load(path)
        self._by_key = {_key(q): q for q in self._entries}

    @staticmethod
    def _load(path: Path) -> dict[str, str | None]:
        if not path.exists():
            return {}
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            # Keep the broken file for the user rather than saving over it.
            backup = path.with_suffix(".corrupt.json")
            os.replace(path, backup)
            log.error("%s is not valid JSON (%s) — moved to %s, starting empty", path, exc, backup)
            return {}

    def get(self, question: str) -> str | None:
        original = self._by_key.get(_key(question))
        return self._entries.get(original) if original else None

    def record(self, question: str, answer: str | None) -> None:
        """Remember a question; an existing answer always wins."""
        if not question.strip() or self.get(question) is not None:
            return
        original = self._by_key.setdefault(_key(question), question)
        self._entries[original] = answer

    def entries(self) -> dict[str, str | None]:
        return dict(self._entries)

    def pending(self) -> list[str]:
        return [q for q, a in self._entries.items() if a is None]

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)
