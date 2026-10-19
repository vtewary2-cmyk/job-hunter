"""LinkedIn scraper — authenticated DOM, minutes-old window.

Two things differ from the original implementation:

1. **Authenticated selectors.**  The previous version queried
   ``.jobs-search__results-list`` / ``.base-search-card__title``, which are the
   *logged-out* public job-search classes.  A CDP-attached session is logged
   in, and LinkedIn serves a completely different tree there, so the old
   selectors matched zero cards.

2. **``f_TPR=r{window}``.**  The window is seconds, and the ``r`` prefix is
   required — a bare ``f_TPR=1800`` is silently ignored by LinkedIn.  A short
   window is the whole strategy: postings surfaced within ~30 minutes have
   single-digit applicant counts.

LinkedIn reshuffles class names regularly.  Rather than hard-code one guess per
field, each field has a list of candidates tried in order, and ``probe()``
dumps what the live page actually contains so selectors can be repaired in
minutes without a debugging session.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page

from src.scrapers.base import BaseScraper, JobDict

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_COMPONENT_KEY_PREFIX = "job-card-component-ref-"
_COMPONENT_CARD = f"div[role='button'][componentkey^='{_COMPONENT_KEY_PREFIX}']"

# Ordered candidates — first selector that yields a node wins.
_CARD_SELECTORS = [
    # 2026 layout (seen 2026-10-09): obfuscated classes; the job id survives
    # only in this attribute. Parsed by _parse_component_card.
    _COMPONENT_CARD,
    "li.scaffold-layout__list-item",
    "div.job-card-container",
    "li.jobs-search-results__list-item",
    ".jobs-search__results-list > li",  # logged-out fallback
]
_TITLE_SELECTORS = [
    "a.job-card-container__link",
    ".job-card-list__title--link",
    ".job-card-list__title",
    ".base-search-card__title",
]
_COMPANY_SELECTORS = [
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__primary-description",
    ".job-card-container__company-name",
    ".base-search-card__subtitle",
]
_LOCATION_SELECTORS = [
    ".job-card-container__metadata-item",
    ".artdeco-entity-lockup__caption",
    ".job-search-card__location",
]
_FOOTER_SELECTORS = [
    ".job-card-container__footer-wrapper",
    ".job-card-list__footer-wrapper",
    "ul.job-card-container__footer-wrapper",
]


async def _first_text(node, selectors: list[str]) -> str | None:
    """Return stripped inner text of the first selector that matches."""
    for sel in selectors:
        try:
            el = await node.query_selector(sel)
        except Exception:
            continue
        if el is None:
            continue
        try:
            text = (await el.inner_text()).strip()
        except Exception:
            continue
        if text:
            return text
    return None


class LinkedInScraper(BaseScraper):
    platform = "linkedin"

    # ------------------------------------------------------------------ #
    # URL construction
    # ------------------------------------------------------------------ #

    # LinkedIn serves job search in pages of 25, addressed by `start`.
    PAGE_SIZE = 25

    @staticmethod
    def build_search_url(
        keywords: list[str], location: str, window_seconds: int, start: int = 0
    ) -> str:
        """Build an authenticated job-search URL for a time window.

        ``f_TPR`` MUST carry the ``r`` prefix; without it LinkedIn ignores the
        filter entirely and returns the unfiltered feed.  ``f_JT=F`` is a
        server-side full-time filter, free of charge.

        ``start`` is the pagination offset.  Driving it through the URL beats
        clicking a next button: no reliance on a pager selector that LinkedIn
        renames, and a failed page does not lose the ones after it.
        """
        kw = quote_plus(" OR ".join(f'"{k}"' for k in keywords))
        loc = quote_plus(location)
        url = (
            "https://www.linkedin.com/jobs/search/"
            f"?keywords={kw}"
            f"&location={loc}"
            f"&f_TPR=r{int(window_seconds)}"
            "&f_JT=F"
            "&sortBy=DD"
        )
        if location.strip().lower() == "india":
            url += "&f_WT=2"  # India-wide search = remote roles only (candidate, 2026-10-15)
        return url if start <= 0 else f"{url}&start={int(start)}"

    # ------------------------------------------------------------------ #
    # Scrape
    # ------------------------------------------------------------------ #

    async def scrape_context(
        self,
        context: BrowserContext,
        keywords: list[str],
        locations: list[str],
        window_seconds: int,
        limit: int | None = None,
        max_pages: int = 8,
    ) -> list[JobDict]:
        """Scrape every location in *locations* using an existing CDP context.

        Paginates.  A single page is 25 results, which was the whole of a
        60-minute window but is a small slice of a 24-hour one — and the cut is
        silent, so the run looks healthy while dropping most of the day.  Worse,
        the filter then runs on those 25 and keeps maybe a handful, so the
        shortfall compounds.
        """
        jobs: list[JobDict] = []
        seen_urls: set[str] = set()

        for location in locations:
            for page_no in range(max_pages):
                start = page_no * self.PAGE_SIZE
                url = self.build_search_url(keywords, location, window_seconds, start)
                page = await context.new_page()
                try:
                    await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
                    # Give the virtualised list a beat, then nudge it to render.
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    await self._nudge_list(page)

                    cards, used = await self._find_cards(page)
                    if not cards:
                        # The results list sometimes renders seconds late; one
                        # patient retry before calling the page empty.
                        await asyncio.sleep(6)
                        await self._nudge_list(page)
                        cards, used = await self._find_cards(page)
                    if not cards:
                        if page_no == 0:
                            log.warning(
                                "linkedin[%s]: no job cards matched any selector", location
                            )
                        else:
                            log.info("linkedin[%s]: page %d empty — end of results",
                                     location, page_no + 1)
                        break

                    added = 0
                    for card in cards:
                        job = await self._parse_card(card, location)
                        if job is None or job["url"] in seen_urls:
                            continue
                        seen_urls.add(job["url"])
                        jobs.append(job)
                        added += 1
                        if limit is not None and len(jobs) >= limit:
                            return jobs

                    log.info(
                        "linkedin[%s] page %d: %d cards via %r, %d new",
                        location, page_no + 1, len(cards), used, added,
                    )

                    # Every URL already seen means LinkedIn is replaying the
                    # last page rather than paging on — the usual signal that
                    # the result set is exhausted.
                    if added == 0:
                        break
                    if len(cards) < self.PAGE_SIZE:
                        break
                except Exception as exc:
                    log.warning("linkedin[%s] page %d: scrape failed: %s", location, page_no + 1, exc)
                    break
                finally:
                    await page.close()

                # Pace the requests. Eight pages across four locations back to
                # back is the shape of a scraper, not a person reading jobs.
                await asyncio.sleep(random.uniform(2.0, 4.5))

        return jobs

    async def _nudge_list(self, page: Page) -> None:
        """LinkedIn lazy-renders the results list; scroll it into existence."""
        for _ in range(6):
            try:
                await page.mouse.wheel(0, 400)
            except Exception:
                break
            await asyncio.sleep(random.uniform(0.2, 0.45))

    async def _find_cards(self, page: Page) -> tuple[list, str | None]:
        for sel in _CARD_SELECTORS:
            try:
                cards = await page.query_selector_all(sel)
            except Exception:
                continue
            if cards:
                return cards, sel
        return [], None

    @staticmethod
    def _salary_lpa(text: str) -> str | None:
        """Card salary as "15-25 LPA". Seen shapes: "₹15L/yr - ₹25L/yr",
        "₹1,500,000/yr", "₹1.2L/mo", "1.2M INR/yr - 1.5M INR/yr".

        Most Indian postings show none; then None, and the CTC gate keeps the job.
        """
        line = next((ln for ln in text.splitlines()
                     if ("₹" in ln or "INR" in ln) and re.search(r"/(yr|mo|month|year)", ln)), None)
        if not line:
            return None
        monthly = bool(re.search(r"/(mo|month)", line))
        amounts = re.findall(r"₹\s*([\d,]+(?:\.\d+)?)\s*([LKM])?", line) or \
            re.findall(r"([\d,]+(?:\.\d+)?)\s*([LKM])?\s*INR", line)
        values = []
        for num, unit in amounts:
            rupees = float(num.replace(",", "")) * {"L": 100_000, "K": 1_000, "M": 1_000_000}.get(unit, 1)
            values.append(round(rupees / 100_000 * (12 if monthly else 1), 2))
        if not values:
            return None
        return f"{min(values):g}-{max(values):g} LPA"

    async def _parse_component_card(self, card, key: str, fallback_location: str) -> JobDict | None:
        """2026 card: no stable classes, so read its text lines in order.

        Lines run: title as announced (may carry "Selected, " / "(Verified job)"),
        then — on some cards only — the title again as shown, then company,
        location, badges and "Easy Apply". Reading lines[1] as the title on a
        card without the repeat turned company into title (seen 2026-10-09).
        """
        job_id = key[len(_COMPONENT_KEY_PREFIX):]
        text = await card.inner_text()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip() != "·"]
        if not job_id.isdigit() or len(lines) < 2:
            return None
        announced = re.sub(r"^selected,\s*", "", lines[0], flags=re.I)
        announced = re.sub(r"\s*\((verified job|with verification)\)\s*$", "", announced, flags=re.I).strip()
        rest = lines[2:] if lines[1].strip().lower() == announced.lower() else lines[1:]
        if not rest:
            return None
        title, company = announced, rest[0]
        location = rest[1] if len(rest) > 1 else fallback_location
        return JobDict(
            title=title,
            company=company or "Unknown",
            platform=self.platform,
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            apply_type="easy_apply" if "easy apply" in text.lower() else "external_form",
            ctc=self._salary_lpa(text),
            yoe_required=None,
            location=location,
            posted_at=datetime.now(tz=IST),
        )

    async def _parse_card(self, card, fallback_location: str) -> JobDict | None:
        key = await card.get_attribute("componentkey")
        if key and key.startswith(_COMPONENT_KEY_PREFIX):
            return await self._parse_component_card(card, key, fallback_location)
        title = await _first_text(card, _TITLE_SELECTORS)
        # The card link carries a visually-hidden copy of the title, so
        # inner_text reads "Title\nTitle". Keep the first line.
        title = title.splitlines()[0].strip() if title else title
        company = await _first_text(card, _COMPANY_SELECTORS)
        location = await _first_text(card, _LOCATION_SELECTORS) or fallback_location

        href = None
        for sel in ("a.job-card-container__link", "a.job-card-list__title--link", "a[href*='/jobs/view/']"):
            try:
                link = await card.query_selector(sel)
            except Exception:
                continue
            if link is not None:
                href = await link.get_attribute("href")
                if href:
                    break

        if not title or not href:
            return None

        if href.startswith("/"):
            href = "https://www.linkedin.com" + href
        # Strip tracking query params so the URL is a stable dedup key.
        href = href.split("?")[0]

        footer = await _first_text(card, _FOOTER_SELECTORS) or ""
        apply_type = "easy_apply" if "easy apply" in footer.lower() else "external_form"

        return JobDict(
            title=title,
            company=company or "Unknown",
            platform=self.platform,
            url=href,
            apply_type=apply_type,
            ctc=None,
            yoe_required=None,
            location=location,
            posted_at=datetime.now(tz=IST),
        )

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    async def probe(self, context: BrowserContext, keywords: list[str], location: str) -> dict:
        """Report which selectors match on the live page.

        Run this when a scrape returns zero jobs.  It tells you whether the
        session died or LinkedIn renamed a class, without a debugger.
        """
        url = self.build_search_url(keywords, location, 86_400)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
            await self._nudge_list(page)

            report: dict = {"url": page.url, "title": await page.title(), "selectors": {}}
            report["authenticated"] = "authwall" not in page.url and "/login" not in page.url

            for name, sels in (
                ("card", _CARD_SELECTORS),
                ("title", _TITLE_SELECTORS),
                ("company", _COMPANY_SELECTORS),
                ("location", _LOCATION_SELECTORS),
                ("footer", _FOOTER_SELECTORS),
            ):
                hits = {}
                for sel in sels:
                    try:
                        hits[sel] = len(await page.query_selector_all(sel))
                    except Exception:
                        hits[sel] = -1
                report["selectors"][name] = hits
            return report
        finally:
            await page.close()

    # ------------------------------------------------------------------ #
    # Detail-page probe
    # ------------------------------------------------------------------ #

    async def probe_detail(
        self,
        context: BrowserContext,
        job_url: str,
        dump_dir,
        open_apply_modal: bool = False,
    ) -> dict:
        """Dump what a job detail page actually contains.

        Phrase matching over rendered text, not class names.  LinkedIn renames
        classes constantly but the words a Premium member reads ("You're a top
        applicant", "Meet the hiring team") are product copy and change far more
        slowly.  The raw HTML is written to disk regardless, because that is the
        only ground truth worth writing selectors against.
        """
        page = await context.new_page()
        try:
            await page.goto(job_url, timeout=45_000, wait_until="domcontentloaded")
            await asyncio.sleep(3.0)
            await self._nudge_list(page)

            html = await page.content()
            stamp = datetime.now(tz=IST).strftime("%Y%m%d-%H%M%S")
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_path = dump_dir / f"{stamp}-detail.html"
            dump_path.write_text(html, encoding="utf-8")

            body = await page.inner_text("body")
            low = body.lower()

            report: dict = {
                "url": page.url,
                "html_dump": str(dump_path),
                "text_chars": len(body),
                # Does the ranking signal exist BEFORE applying?  The whole
                # apply-then-message ordering hangs on this answer.
                "ranking_signal": {
                    phrase: (phrase in low)
                    for phrase in (
                        "top applicant",
                        "top 5%",
                        "top 10%",
                        "top 25%",
                        "top 50%",
                        "how you compare",
                        "applicant insights",
                        "among the first",
                        "premium",
                    )
                },
                # Is there a human to message at all?  Many posts expose nobody.
                "hiring_team": {
                    phrase: (phrase in low)
                    for phrase in (
                        "meet the hiring team",
                        "job poster",
                        "posted by",
                        "send inmail",
                        "message",
                    )
                },
                "apply_button": {},
                "applicant_count_lines": [
                    line.strip()
                    for line in body.splitlines()
                    if "applicant" in line.lower() and len(line.strip()) < 120
                ][:10],
            }

            for label, sel in (
                ("easy_apply", "button.jobs-apply-button"),
                ("apply_any", "button:has-text('Apply')"),
                ("message_button", "button:has-text('Message')"),
                ("connect_button", "button:has-text('Connect')"),
                ("poster_link", "a[href*='/in/']"),
            ):
                try:
                    report["apply_button"][label] = len(await page.query_selector_all(sel))
                except Exception:
                    report["apply_button"][label] = -1

            if open_apply_modal:
                report["apply_form"] = await self._probe_apply_modal(page, dump_dir, stamp)

            return report
        finally:
            await page.close()

    async def _probe_apply_modal(self, page: Page, dump_dir, stamp: str) -> dict:
        """Open the Easy Apply modal, list its fields, then discard it.

        Never submits.  On dismissal LinkedIn offers to save a draft; we take
        Discard so the probe leaves no half-finished application behind.
        """
        out: dict = {"opened": False, "fields": [], "note": ""}
        try:
            btn = await page.query_selector("button.jobs-apply-button")
            if btn is None:
                out["note"] = "no Easy Apply button on this posting"
                return out
            await btn.click()
            await asyncio.sleep(3.0)
            out["opened"] = True

            (dump_dir / f"{stamp}-apply-modal.html").write_text(
                await page.content(), encoding="utf-8"
            )
            out["modal_dump"] = str(dump_dir / f"{stamp}-apply-modal.html")

            for el in await page.query_selector_all(
                "div[role='dialog'] input, div[role='dialog'] select, div[role='dialog'] textarea"
            ):
                out["fields"].append(
                    {
                        "tag": await el.evaluate("e => e.tagName.toLowerCase()"),
                        "type": await el.get_attribute("type"),
                        "id": await el.get_attribute("id"),
                        "name": await el.get_attribute("name"),
                        "required": await el.get_attribute("required") is not None,
                        "label": await el.evaluate(
                            "e => { const l = e.labels && e.labels[0];"
                            " return l ? l.innerText.trim() : (e.getAttribute('aria-label') || ''); }"
                        ),
                    }
                )
        except Exception as exc:
            out["note"] = f"modal probe failed: {exc}"
        finally:
            # Escape, then take Discard if LinkedIn asks about saving a draft.
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(1.0)
                discard = await page.query_selector(
                    "button[data-control-name='discard_application_confirm_btn'],"
                    " button:has-text('Discard')"
                )
                if discard is not None:
                    await discard.click()
                    await asyncio.sleep(1.0)
            except Exception as exc:
                out["note"] += f" | cleanup failed, check for a saved draft: {exc}"
        return out

    # ------------------------------------------------------------------ #
    # BaseScraper compatibility
    # ------------------------------------------------------------------ #

    async def scrape(
        self,
        keywords: list[str],
        locations: list[str],
        limit: int,
        headless: bool,  # noqa: ARG002 — kept for the ABC signature
        user_data_dir: str,  # noqa: ARG002
    ) -> list[JobDict]:
        """Legacy entrypoint — opens its own CDP attach."""
        from src.common.browser import attach

        _browser, context = await attach()
        return await self.scrape_context(context, keywords, locations, 86_400, limit)
