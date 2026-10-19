"""Submit one LinkedIn Easy Apply application inside the poller's browser.

Async, on the context the scraper already holds — the old sync
``LinkedInApplier`` could not run inside the poller's event loop at all.

The guardrail that matters: **a required field the profile cannot answer
aborts the application.**  Nothing prompts (there is no tty under launchd) and
nothing guesses.  The aborted job lands in the tracker as ``manual_review``
with the unanswered question in ``next_steps``, so adding that answer to
``form_profile.json`` is a one-line fix the next run picks up.

Dry run walks every step of the modal, filling and uploading exactly as a live
run would, and discards the draft at the Submit button.  So a dry run proves
the whole form is answerable, not just that a button exists.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from playwright.async_api import BrowserContext, Locator, Page

from src.apply import resume_picker
from src.apply.answers import answer, pick_option
from src.apply.question_bank import QuestionBank
from src.apply.resume_picker import ResumeNotBuilt
from src.apply.trace import NoTrace, Trace

log = logging.getLogger(__name__)

_MAX_STEPS: Final[int] = 10
# LinkedIn moved the modal to a native <dialog data-testid="dialog"> with
# obfuscated classes (seen 2026-10-02); the role=dialog div is the old shape.
_DIALOG: Final[str] = "dialog[data-testid='dialog'], dialog[open], div[role='dialog']"
_EASY_APPLY_BUTTONS: Final[list[str]] = [
    "button.jobs-apply-button",
    "button[aria-label*='Easy Apply']",
]
_DESCRIPTION: Final[list[str]] = [
    "#job-details",
    ".jobs-description__content",
    ".jobs-description",
]
# The 2026 job page has no stable description selector, but its headings are
# product copy: the description runs from "About the job" to the next section.
_DESCRIPTION_START: Final[str] = "About the job"
_DESCRIPTION_ENDS: Final[tuple[str, ...]] = ("Set alert for similar jobs", "About the company")
_SUBMIT: Final = re.compile(r"^\s*submit( application)?\s*$", re.IGNORECASE)
_REVIEW: Final = re.compile(r"^\s*review( your application)?\s*$", re.IGNORECASE)
_NEXT: Final = re.compile(r"^\s*(next|continue( to next step)?)\s*$", re.IGNORECASE)
# Only the confirmation copy. A bare "applied" matches the nav's "Applied jobs"
# link on every page and would record a failed submit as a success.
_SENT: Final = re.compile(r"application (was )?(sent|submitted)", re.IGNORECASE)

# Enumerates the current step's fields and tags each with data-jh so Python can
# address it again.  Labels on LinkedIn carry a visually-hidden copy of the
# text, so "EmailEmail" is halved back to "Email".
_ENUMERATE_JS: Final[str] = r"""
(dialog) => {
  const clean = (t) => {
    t = (t || '').replace(/\s+/g, ' ').replace(/\*|\brequired\b/gi, '').trim();
    const h = t.length / 2;
    return (t.length % 2 === 0 && t.slice(0, h).trim() === t.slice(h).trim()) ? t.slice(0, h).trim() : t;
  };
  // Typeaheads (city) have no <label for>; the question text sits in the
  // field's wrapper, whose componentkey starts "easyApplyFieldFocus".
  const rawLabel = (el) => {
    if (el.labels && el.labels[0]) return el.labels[0].innerText;
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const by = el.getAttribute('aria-labelledby');
    const n = by && document.getElementById(by.split(' ')[0]);
    if (n) return n.innerText;
    const wrap = el.closest('[componentkey^="easyApplyFieldFocus"]');
    const l = wrap && wrap.querySelector('label, legend, p');
    return l ? l.innerText : (el.getAttribute('placeholder') || '');
  };
  const labelOf = (el) => clean(rawLabel(el));
  // Radio options often have no <label for>; fall back to the wrapping
  // label, the parent's text, then the value attribute ("Yes"/"No").
  // The 2026 modal wraps each option in div[role=radio] whose text is the
  // option ("Yes"); the <label for> beside the input is empty.
  const optionOf = (r) => clean((r.closest('[role=radio]') || {}).innerText)
    || clean((r.labels && r.labels[0] || {}).innerText) || clean((r.closest('label') || {}).innerText)
    || clean((r.parentElement || {}).innerText) || r.value || '';
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const req = (el) => el.required || el.getAttribute('aria-required') === 'true'
                      || /\*\s*$/.test((rawLabel(el) || '').trim());
  const out = [];
  let i = 0;
  const tag = (el) => { const id = 'jh' + (i++); el.setAttribute('data-jh', id); return id; };
  for (const fs of dialog.querySelectorAll('fieldset')) {
    const radios = [...fs.querySelectorAll('input[type=radio]')];
    if (!radios.length || !visible(fs)) continue;
    const legend = fs.querySelector('legend');
    const by = fs.getAttribute('aria-labelledby');
    const byNode = by && document.getElementById(by.split(' ')[0]);
    const wrap = fs.closest('[componentkey^="easyApplyFieldFocus"]');
    const wrapText = wrap && wrap.querySelector('label, p, span');
    const groupLabel = (legend && legend.innerText) || (byNode && byNode.innerText)
                       || fs.getAttribute('aria-label') || (wrapText && wrapText.innerText) || '';
    let options = radios.map(optionOf);
    if (options.every(o => !o || o === 'on')) {
      // No per-option labels (Sprinto, 2026-10-15): the group's text lists the
      // options in order after the question, so take its last N lines.
      const lines = (fs.innerText || '').split('\n').map(t => t.trim()).filter(Boolean);
      if (lines.length >= radios.length) options = lines.slice(-radios.length);
    }
    const checked = radios.findIndex(r => r.checked);
    out.push({id: tag(fs), kind: 'radio', label: clean(groupLabel),
              options, filled: checked >= 0,
              value: checked >= 0 ? options[checked] : '',
              required: req(fs) || radios.some(req)});
  }
  for (const el of dialog.querySelectorAll('select')) {
    if (!visible(el)) continue;
    const opts = [...el.options].map(o => o.text.trim());
    const cur = el.selectedIndex >= 0 ? opts[el.selectedIndex] : '';
    out.push({id: tag(el), kind: 'select', label: labelOf(el), options: opts,
              filled: !!cur && !/select an option/i.test(cur), value: cur, required: req(el)});
  }
  const inputs = 'input:not([type=radio]):not([type=checkbox]):not([type=file]):not([type=hidden]), textarea';
  for (const el of dialog.querySelectorAll(inputs)) {
    if (!visible(el)) continue;
    const numeric = el.type === 'number' || el.getAttribute('inputmode') === 'numeric'
                    || /numeric/.test(el.id || '');
    out.push({id: tag(el), kind: numeric ? 'numeric' : 'text', label: labelOf(el), options: [],
              filled: !!el.value.trim(), value: el.value.trim(), required: req(el),
              combobox: el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list'
                        || el.getAttribute('data-testid') === 'typeahead-input'});
  }
  for (const el of dialog.querySelectorAll('input[type=checkbox]')) {
    if (!visible(el) && !visible(el.closest('div') || el)) continue;
    if (req(el) && !el.checked) {
      out.push({id: tag(el), kind: 'checkbox', label: labelOf(el), options: [], filled: false, required: true});
    }
  }
  return out;
}
"""


@dataclass(frozen=True)
class Outcome:
    status: Literal["applied", "would_apply", "manual_review"]
    reason: str
    resume: Path | None = None


class _Abort(Exception):
    """Stop this application; the message becomes the tracker's next_steps."""


async def apply(
    context: BrowserContext,
    job: dict,
    *,
    profile: dict,
    bank: QuestionBank,
    resume_keywords: dict[str, list[str]],
    live: bool,
    trace: Trace | None = None,
) -> Outcome:
    """Run the Easy Apply modal for one job.  Never raises."""
    trace = trace or NoTrace()
    trace.start_job(job)
    outcome = await _apply(context, job, profile, bank, resume_keywords, live, trace)
    trace.event("outcome", status=outcome.status, reason=outcome.reason,
                resume=outcome.resume.name if outcome.resume else None, live=live)
    return outcome


async def _apply(
    context: BrowserContext,
    job: dict,
    profile: dict,
    bank: QuestionBank,
    resume_keywords: dict[str, list[str]],
    live: bool,
    trace: Trace,
) -> Outcome:
    page = await context.new_page()
    resume: Path | None = None
    try:
        await page.goto(job["url"], timeout=45_000, wait_until="domcontentloaded")
        await asyncio.sleep(3.0)
        description = await _description(page)
        resume = resume_picker.pick(
            job.get("title", ""), description, resume_keywords=resume_keywords
        )
        trace.event("resume_picked", resume=resume.name, description_chars=len(description))
        body = await page.inner_text("body")
        industry, about_size = _about_company(body)
        size = about_size or _company_size(body)
        if size is not None and size[1] < MIN_COMPANY_EMPLOYEES:
            band = f"{size[0]:,}-{size[1]:,}"
            return Outcome("manual_review", f"Skipped: small company ({band} employees on LinkedIn)", resume)
        if industry and not _industry_ok(industry):
            return Outcome("manual_review", f"Skipped: industry not in your list ({industry})", resume)
        await _open_modal(page)
        dialog = page.locator(_DIALOG).first
        return await _walk(page, dialog, profile, bank, resume, live, trace)
    except ResumeNotBuilt as exc:
        return Outcome("manual_review", str(exc))
    except _Abort as exc:
        await trace.shot(page, "abort")
        log.info("easy apply aborted for %s @ %s: %s", job.get("title"), job.get("company"), exc)
        await _discard(page)
        return Outcome("manual_review", str(exc), resume)
    except Exception as exc:  # a broken page must not end the run
        log.warning("easy apply failed for %s: %s", job.get("url"), exc, exc_info=True)
        await _discard(page)
        # The reason reaches the Sheet and a phone push: say what to do, keep
        # the exception type for whoever debugs it.
        return Outcome("manual_review", f"Retry later — the form misbehaved ({type(exc).__name__})", resume)
    finally:
        await page.close()


# Industries she has worked in, minus EdTech, plus SaaS (candidate, 2026-10-14).
_INDUSTRY_OK: Final = re.compile(
    r"software|saas|it services|information technology|technology, information|computer and network security"
    r"|human resources|staffing|recruit|payroll|professional employer|workforce"
    r"|business consulting|consulting",  # consulting added 2026-10-15
    re.IGNORECASE,
)
_INDUSTRY_NEVER: Final = re.compile(r"e-?learning|education|edtech|higher ed", re.IGNORECASE)


def _industry_ok(industry: str) -> bool:
    return bool(_INDUSTRY_OK.search(industry)) and not _INDUSTRY_NEVER.search(industry)


def _about_company(body: str) -> tuple[str | None, tuple[int, int] | None]:
    """(industry, employee band) from the job page's "About the company" block:
    "About the company | Name | N followers | Follow | <Industry> | • | 1001-5000 employees"."""
    start = body.find("About the company")
    if start < 0:
        return None, None
    lines = [l.strip() for l in body[start:start + 600].splitlines() if l.strip() and l.strip() != "•"]
    for i, line in enumerate(lines):
        if re.search(r"\bemployees\b", line):
            size = _company_size(line)
            industry = lines[i - 1] if i > 0 and not re.search(r"followers|^follow", lines[i - 1], re.I) else None
            return industry, size
    return None, None


# Established companies only (candidate, 2026-10-11): the job page's "About the
# company" band ("201-500 employees", "10001+ employees") must reach past this.
MIN_COMPANY_EMPLOYEES: Final[int] = 201  # 200+ employees (candidate relaxed it, 2026-10-15)


def _company_size(body: str) -> tuple[int, int] | None:
    """The page's employee band as (low, high); None when LinkedIn shows none."""
    found = re.search(r"\b([\d,]+)\s*(?:[-–]\s*([\d,]+)|(\+))\s*employees\b", body)
    if not found:
        return None
    low = int(found.group(1).replace(",", ""))
    high = int(found.group(2).replace(",", "")) if found.group(2) else 10**9
    return low, high


async def _description(page: Page) -> str:
    for selector in _DESCRIPTION:
        node = page.locator(selector).first
        if await node.count():
            text = (await node.inner_text()).strip()
            if text:
                return text
    body = await page.inner_text("body")
    start = body.find(_DESCRIPTION_START)
    if start < 0:
        return ""
    ends = [i for i in (body.find(m, start) for m in _DESCRIPTION_ENDS) if i > start]
    return body[start : min(ends) if ends else None].strip()


async def _open_modal(page: Page) -> None:
    for selector in _EASY_APPLY_BUTTONS:
        button = page.locator(selector).first
        if await button.count() and await button.is_visible():
            await button.click()
            dialog = page.locator(_DIALOG).first
            await dialog.wait_for(state="visible", timeout=15_000)
            await _wait_for_form(dialog)
            return
    body = (await page.inner_text("body")).lower()
    if "applied" in body and "application" in body:
        raise _Abort("Nothing to do — already applied, or the job no longer offers Easy Apply")
    raise _Abort("Apply on the company site — this job has no Easy Apply button")


async def _wait_for_form(dialog: Locator, timeout_s: float = 15.0) -> None:
    """The dialog opens as a spinner; wait until it has a button to press.

    Reading it ~2 s after opening caught the spinner on a Cvent form
    (2026-10-02 verifier run) and aborted with "no Next button".
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        for name in (_NEXT, _REVIEW, _SUBMIT):
            if await _button(dialog, name):
                await asyncio.sleep(0.5)
                return
        await asyncio.sleep(0.5)
    raise _Abort("Retry later — the application form never finished loading")


async def _walk(
    page: Page,
    dialog: Locator,
    profile: dict,
    bank: QuestionBank,
    resume: Path,
    live: bool,
    trace: Trace,
) -> Outcome:
    resume_chosen = False
    for step in range(1, _MAX_STEPS + 1):
        await _settle(dialog)
        heading = await _step_heading(dialog)
        if not resume_chosen:
            resume_chosen = await _choose_resume(page, dialog, resume, trace)
        try:
            await _fill_step(dialog, profile, bank, trace, step)
        finally:
            trace.event("step", step=step, heading=heading, shot=await trace.shot(dialog, f"step{step}"))

        if await _button(dialog, _SUBMIT):
            if not live:
                trace.event("click", step=step, button="(dry run) discard at Submit")
                await _discard(page)
                return Outcome("would_apply", f"dry run: form complete in {step} steps", resume)
            trace.event("click", step=step, button="Submit application")
            await (await _button(dialog, _SUBMIT)).click()
            outcome = await _confirm_sent(page, resume)
            await trace.shot(page, "after-submit")
            return outcome

        advance = await _button(dialog, _REVIEW) or await _button(dialog, _NEXT)
        if advance is None:
            raise _Abort(f"Needs a look — step {step} has no Next, Review or Submit button")
        before = await dialog.inner_text()
        page_before = _page_counter(before)
        trace.event("click", step=step, button=(await advance.inner_text()).strip())
        await advance.click()
        await asyncio.sleep(1.5)
        # A new "n/m pages" count means LinkedIn accepted this step. The next
        # step's questions can read like errors ("Please enter your current ctc
        # in INR*"), and two steps can share a heading ("Work experience"), so
        # only look for validation messages when the counter stayed put.
        page_after = _page_counter(await dialog.inner_text())
        if page_before and page_after and page_after != page_before:
            continue
        errors = await _errors(dialog)
        if errors or await dialog.inner_text() == before:
            detail = "; ".join(errors) or "the page did not advance"
            trace.event("rejected", step=step, errors=errors or ["did not advance"])
            raise _Abort(f"Needs a look — LinkedIn rejected an answer on step {step}: {detail}")
    raise _Abort(f"Needs a look — the form ran past {_MAX_STEPS} steps")


async def _settle(dialog: Locator, quiet_s: float = 1.5, max_s: float = 10.0) -> None:
    """Wait until the step stops changing before reading its fields.

    Some steps render in parts: Zoca's "Work experience" (2026-10-09) showed the
    work history first and its CTC / notice-period questions a moment later, so
    an early read found no fields and pressed Next on an unfilled page.
    """
    last, quiet, waited = await dialog.inner_text(), 0.0, 0.0
    while waited < max_s and quiet < quiet_s:
        await asyncio.sleep(0.5)
        waited += 0.5
        now = await dialog.inner_text()
        quiet = quiet + 0.5 if now == last else 0.0
        last = now


def _page_counter(text: str) -> str | None:
    """The modal's "3/7 pages" progress line, or None if it has none."""
    found = re.search(r"\b(\d+/\d+) pages\b", text)
    return found.group(1) if found else None


async def _step_heading(dialog: Locator) -> str:
    """The form's own name for this step ("Contact info", "Resume", ...)."""
    lines = [line.strip() for line in (await dialog.inner_text()).splitlines() if line.strip()]
    pages = next((i for i, line in enumerate(lines) if re.search(r"\d+/\d+ pages", line)), None)
    return lines[pages + 1] if pages is not None and pages + 1 < len(lines) else ""


async def _choose_resume(page: Page, dialog: Locator, resume: Path, trace: Trace) -> bool:
    """Select this candidate's current resume on the resume step.

    LinkedIn preselects whatever was used last — on Yash's account a 2024 PDF —
    so the step is never left as found.  If the file was uploaded before, its
    card is listed by file name and is clicked; otherwise it is uploaded, which
    LinkedIn selects automatically.  Returns False on steps without a resume.
    """
    upload = dialog.get_by_role("button", name=re.compile(r"^upload resume$", re.I))
    if not await upload.count():
        return False
    listed = dialog.get_by_text(resume.name, exact=True)
    if await listed.count():
        await listed.first.click()
        trace.event("resume", action="selected already-uploaded", file=resume.name)
        log.info("selected the already-uploaded %s", resume.name)
    else:
        async with page.expect_file_chooser() as chooser:
            await upload.first.click()
        await (await chooser.value).set_files(str(resume))
        await dialog.get_by_text(resume.name, exact=True).first.wait_for(timeout=30_000)
        trace.event("resume", action="uploaded", file=resume.name)
        log.info("uploaded %s", resume.name)
    await asyncio.sleep(1.0)
    return True


def _resolve(field: dict, profile: dict, bank: QuestionBank) -> tuple[str | None, str]:
    """answers.json first (the candidate's word), then the profile rules.

    Returns the value and where it came from, for the trace.
    """
    kind = "choice" if field["kind"] in ("radio", "select") else field["kind"]
    if kind == "checkbox":
        return None, "unanswered"
    remembered = bank.get(field["label"])
    if remembered is not None:
        value = pick_option(remembered, field["options"]) if kind == "choice" else remembered
        # A number saved for a question some form asks as Yes/No ("What is your
        # total years of experience?" -> Yes / No, Razorpay 2026-10-14).
        if value is None and kind == "choice" and {o.lower() for o in field["options"]} >= {"yes", "no"}:
            if re.fullmatch(r"\s*\d+(\.\d+)?\s*", remembered or "") and float(remembered) > 0:
                value = pick_option("Yes", field["options"])
        return value, "answers.json"
    value = answer(field["label"], kind, profile, field["options"])
    return value, "rule" if value is not None else "unanswered"


def _privacy_consent(step_text: str) -> bool:
    """A required box on a step that only asks to acknowledge a privacy notice."""
    text = step_text.lower()
    return bool(re.search(r"privacy (notice|policy|statement)", text)) and not re.search(
        r"terms (and|&) conditions|terms of (use|service)", text
    )


async def _fill_step(dialog: Locator, profile: dict, bank: QuestionBank, trace: Trace, step: int) -> None:
    unanswered: list[str] = []
    step_text = await dialog.inner_text()
    trace.event("step_text", step=step, text=step_text[:4000])
    # "Have you worked at <company>? (Select all that apply)" checkbox groups:
    # her resume lists every employer, and none is the asking company, so tick
    # "Not Applicable" in any such group that has nothing ticked (LinkedIn, 2026-10-14).
    ticked = await dialog.evaluate("""(d) => {
        const text = (b) => {
            let e = b;
            for (let i = 0; i < 4 && e; i++, e = e.parentElement) {
                const t = (e.innerText || '').trim();
                if (t) return t;
            }
            return (b.labels && b.labels[0] && b.labels[0].innerText) || '';
        };
        let n = 0;
        for (const b of d.querySelectorAll('input[type=checkbox]')) {
            if (!/^\\s*(not applicable|none of the above|n\\/a)\\s*$/i.test(text(b))) continue;
            // Smallest ancestor that carries the question = this box's group.
            let g = b.parentElement;
            while (g && g !== d && !/worked (at|for)/i.test(g.innerText || '')) g = g.parentElement;
            if (!g || g === d) continue;
            const boxes = [...g.querySelectorAll('input[type=checkbox]')];
            if (boxes.some(x => x.checked)) continue;
            b.click(); n++;
        }
        // "I understand my application needs a resume … — Confirmed": true, the
        // resume is always attached (LinkedIn, 2026-10-14). Nothing broader.
        for (const b of d.querySelectorAll('input[type=checkbox]')) {
            if (b.checked || !/^\\s*(confirmed|i confirm|i understand)\\s*$/i.test(text(b))) continue;
            let g = b.parentElement;
            while (g && g !== d && !/provide a resume|career profile/i.test(g.innerText || '')) g = g.parentElement;
            if (g && g !== d) { b.click(); n++; }
        }
        return n; }""")
    if ticked:
        trace.event("field", step=step, label="worked at this company?", kind="checkbox", required=True,
                    before="", entered=f"Not Applicable x{ticked}", source="resume", options=[])
    # Multi-select tick-box questions ("Which industry did your last organizations
    # belong to?"): for every question on this step with a saved answer, tick the
    # unchecked box whose own label is exactly that answer (Sprinto, 2026-10-15).
    wanted = sorted({a.strip() for q, a in bank.entries().items()
                     if a and q.strip() and q.strip() in step_text and len(a) < 60})
    if wanted:
        ticked_opts = await dialog.evaluate("""(d, wanted) => {
            const label = (b) => { let e = b;
                for (let i = 0; i < 4 && e; i++, e = e.parentElement) { const t = (e.innerText || '').trim(); if (t) return t; }
                return ''; };
            const hits = [];
            for (const b of d.querySelectorAll('input[type=checkbox]')) {
                const t = label(b);
                if (!b.checked && wanted.includes(t)) { b.click(); hits.push(t); }
            }
            return hits; }""", wanted)
        for opt in ticked_opts:
            trace.event("field", step=step, label="(tick-box)", kind="checkbox", required=True,
                        before="", entered=opt, source="answers.json", options=[])
    if _privacy_consent(step_text):
        # Freshworks' "I consent" box carries no required attribute, so the
        # enumerator never lists it; tick unchecked boxes on privacy-notice steps.
        for box in await dialog.locator("input[type=checkbox]").all():
            if not await box.is_checked():
                # The real input sits off-screen behind a styled box; click it in-page.
                await box.evaluate("el => el.click()")
                trace.event("field", step=step, label="privacy notice consent", kind="checkbox",
                            required=True, before="", entered="checked", source="privacy consent", options=[])
    for field in await dialog.evaluate(_ENUMERATE_JS):
        if field["kind"] == "checkbox" and _privacy_consent(step_text):
            # The candidate agreed (2026-10-09) to privacy-notice confirmations;
            # general Terms & Conditions are excluded by _privacy_consent.
            await dialog.locator(f"[data-jh='{field['id']}']").check(force=True)
            trace.event("field", step=step, label=field["label"], kind="checkbox", required=True,
                        before="", entered="checked", source="privacy consent", options=[])
            continue
        value, source = _resolve(field, profile, bank)
        if field["label"] and (value is not None or (field["required"] and not field["filled"])):
            bank.record(field["label"], value)
        if value is None and field["filled"]:
            source = "prefill"
        trace.event("field", step=step, label=field["label"], kind=field["kind"],
                    required=field["required"], before=field.get("value", ""),
                    entered=value if value is not None else field.get("value", ""),
                    source=source, options=field["options"][:12])
        if value is None:
            # LinkedIn's own prefill stands when the profile has no opinion.
            if field["required"] and not field["filled"]:
                unanswered.append(
                    field["label"] or f"unlabelled {field['kind']} {field['options'][:4]}"
                )
            continue
        # The profile wins over a prefill: LinkedIn prefilled "Phone country
        # code" with Andorra (+376), the first option, on 2026-10-02.
        if value != field.get("value"):
            await _set(dialog.locator(f"[data-jh='{field['id']}']"), field, value)
    if unanswered:
        raise _Abort(
            "Needs your answer (tell Claude; saved to answers.json): " + " | ".join(unanswered)
        )


async def _set(node: Locator, field: dict, value: str) -> None:
    if field["kind"] == "select":
        await node.select_option(label=value)
    elif field["kind"] == "radio":
        index = field["options"].index(value)
        radio = node.locator("input[type=radio]").nth(index)
        wrapper = radio.locator("xpath=ancestor::*[@role='radio'][1]")
        try:
            await (wrapper if await wrapper.count() else radio).click(force=True, timeout=5_000)
        except Exception:
            # Hidden off-screen input (Sprinto, 2026-10-15): click it in-page.
            await radio.evaluate("el => el.click()")
    else:
        await node.fill(value)
        if field.get("combobox"):
            await _pick_suggestion(node, value)


async def _pick_suggestion(node: Locator, value: str) -> None:
    """Typeaheads (city) only accept a value picked from their suggestion list."""
    await asyncio.sleep(1.5)
    options = node.page.locator("[role='option']")
    for option in await options.all():
        if value.lower() in (await option.inner_text()).lower() and await option.is_visible():
            await option.click()
            return
    await node.press("ArrowDown")
    await node.press("Enter")


async def _button(dialog: Locator, name: re.Pattern[str]) -> Locator | None:
    for candidate in await dialog.locator("button").all():
        label = (await candidate.get_attribute("aria-label") or "").strip()
        text = (await candidate.inner_text()).strip()
        if (name.match(label) or name.match(text)) and await candidate.is_visible():
            return candidate
    return None


_ERROR_COPY: Final = re.compile(r"required|please (enter|select|make)|invalid|enter a (valid|whole|decimal)", re.I)


async def _errors(dialog: Locator) -> list[str]:
    """Visible validation messages. The 2026 modal has no stable error class,
    so match its copy ("This field is required") line by line instead."""
    lines = (await dialog.inner_text()).splitlines()
    return sorted({" ".join(line.split()) for line in lines if _ERROR_COPY.search(line) and len(line) < 160})


async def _confirm_sent(page: Page, resume: Path) -> Outcome:
    for _ in range(10):
        await asyncio.sleep(1.0)
        if _SENT.search(await page.inner_text("body")):
            await _dismiss(page)
            return Outcome("applied", f"submitted with {resume.name}", resume)
    return Outcome("manual_review", "clicked Submit but saw no confirmation — check LinkedIn", resume)


async def _dismiss(page: Page) -> None:
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
    except Exception as exc:
        log.debug("dismiss failed: %s", exc)


async def _discard(page: Page) -> None:
    """Close the modal and take Discard, so no half-filled draft is left behind."""
    try:
        if not await page.locator(_DIALOG).count():
            return
        await page.keyboard.press("Escape")
        await asyncio.sleep(1.0)
        discard = page.locator(
            "button[data-control-name='discard_application_confirm_btn'], button:has-text('Discard')"
        ).first
        if await discard.count():
            await discard.click()
            await asyncio.sleep(0.8)
    except Exception as exc:
        log.warning("could not discard the Easy Apply draft — check LinkedIn: %s", exc)
