"""BaseApplier ABC — shared apply flow logic for all platform appliers."""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar, Literal

logger = logging.getLogger(__name__)

from src.common import home

# Path to the shared form profile used by all appliers
_FORM_PROFILE_PATH = home.form_profile_path()

# Known ATS URL patterns that always trigger manual_review
_KNOWN_ATS_PATTERNS: list[str] = [
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "wd3.myworkday",
    "icims.com",
]


def _load_form_profile(path: Path = _FORM_PROFILE_PATH) -> dict:
    """Load and return the form profile from disk."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _slugify(label: str) -> str:
    """Convert a field label to a snake_case profile key.

    Example: "Current Notice Period" → "current_notice_period"
    """
    import re
    slug = label.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug


class BaseApplier(ABC):
    """Abstract base for all platform-specific Playwright appliers.

    Subclasses must set the ``platform`` class variable and implement
    ``_detect_apply_type`` and ``_run_apply_flow``.
    """

    platform: ClassVar[str]

    def apply(
        self,
        job: dict,
        profile: dict,
        settings,
        live: bool,
    ) -> Literal["applied", "manual_review"]:
        """Execute the full apply flow for one job.

        Flow:
        1.  Load profile + learned_mappings from form_profile.json.
        2.  Open Playwright context (platform's Chrome profile).
        3.  Navigate to job URL.
        4.  detect_apply_type() → "easy_apply" | "external_form" | "unknown_ats".
        5.  If "unknown_ats": return "manual_review".
        6.  For each form field: fuzzy-match learned_mappings, auto-fill or
            prompt + persist.
        7.  Upload resume.
        8.  dry-run: log payload, return "applied".
        9.  live: click submit, return "applied".
        10. On any exception: return "manual_review".

        Args:
            job:      Job dict (contains at minimum ``url`` and ``company``).
            profile:  Caller may pass a pre-loaded profile; this method reloads
                      from disk so that the most recent learned_mappings are used.
            settings: ``Settings`` dataclass (provides ``resume_pdf``,
                      ``chrome_profiles``, ``headless``).
            live:     True → actually submit the form.  False → dry-run.

        Returns:
            "applied" on success (dry-run or live).
            "manual_review" when the apply type is unknown or any exception occurs.
        """
        try:
            # Always reload from disk to pick up latest learned_mappings
            profile = _load_form_profile()
            return self._apply_inner(job, profile, settings, live)
        except Exception as exc:
            logger.warning(
                "apply() raised for %s @ %s: %s — returning manual_review",
                job.get("title"),
                job.get("company"),
                exc,
                exc_info=True,
            )
            return "manual_review"

    def _apply_inner(
        self,
        job: dict,
        profile: dict,
        settings,
        live: bool,
    ) -> Literal["applied", "manual_review"]:
        """Inner apply logic; exceptions propagate up to ``apply()``."""
        from playwright.sync_api import sync_playwright  # type: ignore[import]

        from src.common.browser import CDP_PORT

        url: str = job["url"]

        with sync_playwright() as pw:
            # Attach to the same long-lived Chrome the scraper uses. Launching a
            # second browser would not carry the LinkedIn session, and Chrome
            # >= 136 refuses remote debugging on the default profile anyway.
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            context = browser.contexts[0]
            page = context.new_page()

            try:
                page.goto(url, timeout=30_000, wait_until="domcontentloaded")

                apply_type = self._detect_apply_type(page, url)
                if apply_type == "unknown_ats":
                    logger.info("Unknown ATS for %s — manual_review", url)
                    return "manual_review"

                result = self._run_apply_flow(page, job, profile, settings, live)
                return result
            finally:
                # Close the page, never the context: the browser is shared and
                # long-lived, and closing it would kill the logged-in session.
                page.close()

    # ------------------------------------------------------------------
    # Abstract hooks — each platform implements these
    # ------------------------------------------------------------------

    @abstractmethod
    def _detect_apply_type(
        self, page, url: str
    ) -> Literal["easy_apply", "external_form", "unknown_ats"]:
        """Inspect the loaded page and return the detected apply type."""

    @abstractmethod
    def _run_apply_flow(
        self,
        page,
        job: dict,
        profile: dict,
        settings,
        live: bool,
    ) -> Literal["applied", "manual_review"]:
        """Execute the platform-specific form-fill + submission flow."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _fuzzy_match_mapping(
        self,
        label: str,
        learned_mappings: dict,
    ) -> str | None:
        """Return the profile key for a form field label.

        Matching strategy:
        1. Exact match on lowercased label.
        2. Check if any mapping key is a substring of the lowercased label.

        Args:
            label:            Raw field label extracted from the form.
            learned_mappings: The ``learned_mappings`` dict from form_profile.

        Returns:
            Profile key string, or ``None`` if no match found.
        """
        norm = label.lower().strip()

        # 1. Exact match
        if norm in learned_mappings:
            return learned_mappings[norm]

        # 2. Substring match — mapping key appears inside the label
        for key, profile_key in learned_mappings.items():
            if key in norm:
                return profile_key

        return None

    def _fill_field_interactive(
        self,
        page,
        field_element,
        label: str,
        profile: dict,
        profile_path: Path = _FORM_PROFILE_PATH,
    ) -> str:
        """Resolve a field value: auto-fill if known, else prompt + persist.

        Args:
            page:          Current Playwright page (used only by subclasses that
                           need to call page.fill(); unused in the base).
            field_element: The DOM element handle for the input field.
            label:         Raw label text from the form.
            profile:       Loaded form profile (mutated in-place when a new field
                           is discovered and the user provides a value).
            profile_path:  Where to persist the updated profile.

        Returns:
            The value that should be filled into the field.
        """
        learned_mappings: dict = profile.get("learned_mappings", {})
        profile_key = self._fuzzy_match_mapping(label, learned_mappings)

        if profile_key and profile_key in profile:
            value = profile[profile_key]
            logger.debug("Auto-fill '%s' → profile[%s] = %r", label, profile_key, value)
            return str(value) if value is not None else ""

        # New field — prompt user
        print(f"\nNew form field encountered: '{label}'")
        value = input(f"Enter your answer for '{label}': ").strip()

        # Persist value + mapping
        key = _slugify(label)
        profile[key] = value
        profile.setdefault("learned_mappings", {})[label.lower().strip()] = key
        self._write_form_profile_atomic(profile, profile_path)
        logger.info("Persisted new field '%s' → key '%s'", label, key)

        return value

    def _write_form_profile_atomic(
        self,
        profile: dict,
        profile_path: Path,
    ) -> None:
        """Write the profile atomically: write to .tmp, then os.replace().

        Verifies the written JSON parses cleanly before replacing the live file.

        Args:
            profile:      The full profile dict to serialise.
            profile_path: Destination path for form_profile.json.
        """
        tmp_path = profile_path.with_suffix(".json.tmp")
        serialised = json.dumps(profile, indent=2, ensure_ascii=False)

        # Verify it round-trips cleanly before touching the live file
        json.loads(serialised)

        tmp_path.write_text(serialised, encoding="utf-8")
        os.replace(tmp_path, profile_path)
        logger.debug("form_profile.json written atomically via %s", tmp_path)

    def _upload_resume(self, page, resume_path: Path) -> bool:
        """Attempt to locate a file-upload input and set the resume PDF.

        Returns True if a file input was found and the resume was attached,
        False otherwise (resume upload is best-effort; not fatal).
        """
        try:
            file_input = page.query_selector("input[type='file']")
            if file_input and resume_path.exists():
                file_input.set_input_files(str(resume_path))
                logger.debug("Uploaded resume: %s", resume_path)
                return True
        except Exception as exc:
            logger.warning("Resume upload failed: %s", exc)
        return False

    def _log_submission_payload(self, job: dict, profile: dict) -> None:
        """Log the field → value pairs that would be submitted (dry-run only)."""
        logger.info(
            "[DRY-RUN] Submission payload for %s @ %s:\n%s",
            job.get("title"),
            job.get("company"),
            json.dumps(
                {
                    k: v
                    for k, v in profile.items()
                    if k != "learned_mappings"
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
