"""Base scraper ABC and shared types.

Login is no longer a scraper concern.  Sessions live in a dedicated Chrome
profile (``$JOBHUNTER_HOME/chrome``) that the user signs into once; the scrapers
attach to that browser over CDP and inherit whatever it is already logged into.
That removed the credential vault, the per-platform ``login()`` implementations,
and the re-login retry paths along with it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar, Literal, TypedDict

from playwright.async_api import Page


class JobDict(TypedDict):
    title: str
    company: str
    platform: str
    url: str
    apply_type: Literal["easy_apply", "external_form"]
    ctc: str | None
    yoe_required: str | None
    location: str | None
    posted_at: datetime


class BaseScraper(ABC):
    platform: ClassVar[str]
    LOGIN_INDICATORS: ClassVar[list[str]] = ["/login", "authwall", "/checkpoint"]

    @abstractmethod
    async def scrape(
        self,
        keywords: list[str],
        locations: list[str],
        limit: int,
        headless: bool,
        user_data_dir: str,
    ) -> list[JobDict]:
        """Scrape job listings and return a list of JobDicts."""

    async def is_logged_out(self, page: Page) -> bool:
        """Return True if the current URL looks like a login / auth wall."""
        url = page.url.lower()
        return any(indicator in url for indicator in self.LOGIN_INDICATORS)
