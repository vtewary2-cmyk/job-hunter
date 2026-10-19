"""Daily HTML email digest builder and sender.

Usage
-----
::

    from src.notify.digest import build_digest, send_digest
    html = build_digest(rows, skipped)
    send_digest(html, settings)
"""

from __future__ import annotations

import smtplib
from collections import Counter
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _fmt_ts(ts_str: str) -> str:
    """Format an ISO 8601 timestamp string into a human-readable IST string.
    Returns the raw string unchanged if parsing fails.
    """
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str).astimezone(IST)
        return dt.strftime("%d %b %Y %H:%M IST")
    except (ValueError, TypeError):
        return ts_str


def _css() -> str:
    return """
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
             background: #f4f6f9; margin: 0; padding: 20px; color: #333; }
      .container { max-width: 700px; margin: 0 auto; background: #fff;
                   border-radius: 8px; overflow: hidden;
                   box-shadow: 0 2px 8px rgba(0,0,0,.08); }
      .header { background: #1a1a2e; color: #fff; padding: 28px 32px; }
      .header h1 { margin: 0 0 6px; font-size: 22px; font-weight: 700; }
      .header p  { margin: 0; font-size: 14px; opacity: .75; }
      .section   { padding: 24px 32px; border-bottom: 1px solid #eee; }
      .section:last-child { border-bottom: none; }
      h2 { font-size: 16px; font-weight: 600; margin: 0 0 14px; color: #1a1a2e; }
      .attention-banner { background: #fff7ed; border-left: 4px solid #f59e0b;
                          padding: 12px 16px; border-radius: 0 6px 6px 0;
                          margin-bottom: 18px; font-size: 13px; color: #92400e; }
      table { width: 100%; border-collapse: collapse; font-size: 13px; }
      th { background: #f8f9fa; text-align: left; padding: 9px 12px;
           font-weight: 600; color: #555; border-bottom: 2px solid #dee2e6; }
      td { padding: 9px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
      tr:last-child td { border-bottom: none; }
      code { background: #f3f4f6; padding: 2px 6px; border-radius: 4px;
             font-size: 11px; font-family: 'SFMono-Regular', Consolas, monospace;
             color: #d63031; white-space: pre-wrap; word-break: break-all; }
      .pill { display: inline-block; padding: 2px 8px; border-radius: 12px;
              font-size: 11px; font-weight: 600; }
      .pill-applied          { background: #d1fae5; color: #065f46; }
      .pill-manual           { background: #fef3c7; color: #92400e; }
      .pill-interview        { background: #dbeafe; color: #1e40af; }
      .pill-offer            { background: #ede9fe; color: #5b21b6; }
      .pill-rejected         { background: #fee2e2; color: #991b1b; }
      .funnel { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 4px; }
      .funnel-item { flex: 1; min-width: 100px; background: #f8f9fa;
                     border-radius: 8px; padding: 14px 16px; text-align: center; }
      .funnel-item .count { font-size: 28px; font-weight: 700; color: #1a1a2e; }
      .funnel-item .label { font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: .5px; }
      .footer { background: #f8f9fa; padding: 16px 32px; font-size: 12px; color: #888; }
      a { color: #2563eb; text-decoration: none; }
      a:hover { text-decoration: underline; }
      .retry-note { margin-top: 10px; font-size: 12px; color: #555; }
    </style>
    """


def _header_section(rows: list[dict], now: datetime) -> str:
    counts = Counter(r.get("status", "") for r in rows)
    applied = counts.get("applied", 0)
    found = len(rows)
    manual = counts.get("manual_review", 0)

    date_str = now.strftime("%A, %d %B %Y — %H:%M IST")

    if found == 0:
        summary_line = "No new jobs found today."
    else:
        parts = [f"<strong>{found}</strong> found"]
        if applied:
            parts.append(f"<strong>{applied}</strong> auto-applied")
        if manual:
            parts.append(f"<strong>{manual}</strong> need manual review")
        summary_line = " · ".join(parts)

    return f"""
    <div class="header">
      <h1>Job Hunter — Daily Digest</h1>
      <p>{date_str}</p>
      <p style="margin-top:10px; font-size:15px;">{summary_line}</p>
    </div>
    """


def _needs_attention_section(skipped: dict, manual_review_rows: list[dict]) -> str:
    """Render 'Needs Your Attention' section.

    Only rendered when there is at least one unretried skipped platform
    OR at least one manual_review job row.
    """
    platforms = [p for p in skipped.get("platforms", []) if not p.get("retried", False)]
    jobs = [j for j in skipped.get("jobs", []) if not j.get("retried", False)]

    if not platforms and not manual_review_rows and not jobs:
        return ""

    html = """
    <div class="section">
      <h2>⚠ Needs Your Attention</h2>
      <div class="attention-banner">
        These items require manual intervention before the pipeline can process them.
      </div>
    """

    # --- Skipped platforms table ---
    if platforms:
        html += "<h3 style='font-size:14px; margin:0 0 10px;'>Skipped Platforms</h3>"
        html += """
        <table>
          <thead>
            <tr>
              <th>Platform</th>
              <th>Reason</th>
              <th>Time</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
        """
        for p in platforms:
            platform = p.get("platform", "")
            reason = p.get("reason", "")
            ts = _fmt_ts(p.get("ts", ""))
            cmd = f"python -m src.run_daily --retry-platform {platform}"
            html += f"""
            <tr>
              <td><strong>{platform}</strong></td>
              <td>{reason}</td>
              <td>{ts}</td>
              <td><code>{cmd}</code></td>
            </tr>
            """
        html += "</tbody></table><br>"

    # --- Manual review jobs table ---
    # Merge skipped jobs list with current manual_review rows (dedup by id)
    seen_ids: set[str] = set()
    all_manual: list[dict] = []

    for j in jobs:
        jid = j.get("id", "")
        if jid not in seen_ids:
            seen_ids.add(jid)
            # skipped job entry shape: id, company, title, url, reason, ts, retried
            all_manual.append({
                "title": j.get("title", ""),
                "company": j.get("company", ""),
                "platform": j.get("platform", ""),
                "url": j.get("url", ""),
                "reason": j.get("reason", ""),
            })

    for r in manual_review_rows:
        rid = r.get("id", "")
        if rid not in seen_ids:
            seen_ids.add(rid)
            all_manual.append({
                "title": r.get("title", ""),
                "company": r.get("company", ""),
                "platform": r.get("platform", ""),
                "url": r.get("url", ""),
                "reason": "manual_review",
            })

    if all_manual:
        html += "<h3 style='font-size:14px; margin:0 0 10px;'>Manual Review Jobs</h3>"
        html += """
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Company</th>
              <th>Platform</th>
              <th>URL</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
        """
        for j in all_manual:
            url = j.get("url", "")
            url_cell = f'<a href="{url}">link</a>' if url else "—"
            html += f"""
            <tr>
              <td>{j.get("title", "")}</td>
              <td>{j.get("company", "")}</td>
              <td>{j.get("platform", "")}</td>
              <td>{url_cell}</td>
              <td>{j.get("reason", "")}</td>
            </tr>
            """
        html += "</tbody></table>"
        html += """
        <p class="retry-note">
          After applying manually, run:
          <code>python -m src.run_daily --retry-skipped</code>
        </p>
        """

    html += "</div>"
    return html


def _funnel_section(rows: list[dict]) -> str:
    counts = Counter(r.get("status", "") for r in rows)

    def _item(label: str, status: str, pill_class: str) -> str:
        n = counts.get(status, 0)
        return f"""
        <div class="funnel-item">
          <div class="count">{n}</div>
          <div class="label">{label}</div>
        </div>
        """

    return f"""
    <div class="section">
      <h2>Pipeline Funnel</h2>
      <div class="funnel">
        {_item("Scraped", "scraped", "pill")}
        {_item("Applied", "applied", "pill-applied")}
        {_item("Manual Review", "manual_review", "pill-manual")}
        {_item("Interview", "interview_scheduled", "pill-interview")}
        {_item("Offer", "offer", "pill-offer")}
        {_item("Rejected", "rejected", "pill-rejected")}
      </div>
    </div>
    """


def _footer_section(now: datetime) -> str:
    ts = now.strftime("%d %b %Y %H:%M IST")
    return f"""
    <div class="footer">
      Generated by job-hunter &bull; {ts}
    </div>
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_digest(rows: list[dict], skipped: dict) -> str:
    """Build the HTML email body for the daily digest.

    Parameters
    ----------
    rows:
        All job rows from ``data_store.read_all()`` (or a filtered subset).
        Used to compute run summary counts and the pipeline funnel.
    skipped:
        Dict with keys ``"platforms"`` and ``"jobs"``, each a list of entries
        from ``run_state.get_unretried_*()`` calls (``retried=false`` entries).
        Retried items are filtered out inside this function as a safety net.

    Returns
    -------
    str
        Complete HTML string with inline CSS, suitable for sending as an
        ``text/html`` MIME part.
    """
    now = _now_ist()

    manual_review_rows = [r for r in rows if r.get("status") == "manual_review"]

    body = (
        "<!DOCTYPE html>"
        "<html lang='en'>"
        "<head><meta charset='UTF-8'>"
        f"{_css()}"
        "</head>"
        "<body>"
        "<div class='container'>"
        f"{_header_section(rows, now)}"
        f"{_needs_attention_section(skipped, manual_review_rows)}"
        f"{_funnel_section(rows)}"
        f"{_footer_section(now)}"
        "</div>"
        "</body>"
        "</html>"
    )
    return body


def send_digest(html: str, settings) -> None:
    """Send the digest email via Gmail SMTP TLS.

    Parameters
    ----------
    html:
        HTML body produced by :func:`build_digest`.
    settings:
        A :class:`src.config.Settings` instance.  Uses
        ``settings.smtp_host``, ``settings.smtp_port``,
        ``settings.gmail_app_password``, and ``settings.digest_recipient``.
    """
    now = _now_ist()
    subject = f"Job Hunter — Daily Digest [{now.strftime('%Y-%m-%d')}]"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Job Hunter <{settings.digest_recipient}>"
    recipients = [settings.digest_recipient, *getattr(settings, "also_to", [])]
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(settings.digest_recipient, settings.gmail_app_password)
        server.sendmail(settings.digest_recipient, recipients, msg.as_string())
