"""Read-only Jobs diagnostics and operations markup. No remote calls on page load."""

from html import escape
from datetime import datetime, timedelta, timezone

import config
from delivery.jobs_discovery import discovery_report


# Human-readable, privacy-safe explanations derived only from persisted reason codes.
# This report never calls Advertio or includes source text, API bodies or credentials.
JOB_REASON_TEXT = {
    "ai_not_processed_joblist": "This message has not completed Jobs classification and AI extraction.",
    "not_verified_employment_listing": "AI did not verify that this is an actual employment listing.",
    "bot_sender_not_allowed": "Messages authored by bots are not eligible for Jobs ingest.",
    "insufficient_cleaned_description": "Cleaned job description is missing or shorter than 20 characters.",
    "description_exceeds_2000_no_truncation": "Cleaned description exceeds Advertio's 2,000-character limit.",
    "description_exceeds_2000_with_compensation_label": "Description exceeds 2,000 characters after the compensation label.",
    "employment_type_without_source_evidence": "Extracted employment type is not supported by the source text.",
    "other_requires_identified_occupation": "Category 'other' requires an identifiable job title.",
    "missing_canadian_city": "A Canadian city was not extracted.",
    "unknown_province": "Province was not found in the Advertio Canadian location catalog.",
    "city_search_truncated": "Advertio city search returned too many matches to validate safely.",
    "unresolved_or_ambiguous_city": "City could not be matched uniquely in the Advertio Canadian city catalog.",
    "city_without_source_evidence": "Extracted city does not appear in the cleaned source text.",
    "title_exceeds_200": "Generated job title exceeds Advertio's 200-character limit.",
    "invalid_original_message_id": "Original Telegram message ID is missing or invalid.",
    "missing_verified_contact_or_source_url": "A verifiable Telegram post URL or original author contact is missing.",
    "retry_limit_exhausted": "The allowed number of delivery evaluations has been exhausted.",
    "invalid_source_date": "Original Telegram post date is missing or invalid.",
    "future_source_date": "Original Telegram post date is in the future.",
    "stale_source_post": "Original Telegram post is outside the allowed freshness window.",
    "jobs_schema_changed": "Advertio Jobs attribute schema differs from the expected options.",
    "transport_or_catalog_error": "Advertio transport or catalog request failed; exact endpoint is not persisted.",
    "unexpected_delivery_error": "The Jobs worker encountered an unexpected error; inspect sanitized system logs.",
    "source_not_approved": "Legacy source restriction blocked this record; this restriction was removed in the current code.",
}
AI_REASON_TEXT = {
    "invalid_provider_output": "AI provider returned invalid or incomplete structured data.",
    "category_mismatch": "AI output category does not match the classified Jobs category.",
    "skipped:category_disabled:joblist": "Jobs AI extraction was disabled when this message was processed.",
    "skipped:no_text": "No usable text was available for AI extraction.",
    "skipped:invalid_classification_category": "Classification category was not supported by AI extraction.",
}
DATE_CODES = {"invalid_source_date", "future_source_date", "stale_source_post"}
LOCATION_CODES = {
    "missing_canadian_city", "unknown_province", "city_search_truncated",
    "unresolved_or_ambiguous_city", "city_without_source_evidence",
}


def _field_value(row, field):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        return "missing"
    # Only a short field value, never an ad's full text or contact information.
    return repr(value.strip()[:60])


def _queue_diagnostic(row):
    code = row.get("error_code") or ""
    status = row.get("status") or ""
    if row.get("remote_status") == "PendingReview" and status in {"sent", "already_existed"}:
        return "Advertio moderation", "Accepted by Advertio; pending review, not rejected."
    if code.startswith("invalid_or_missing_"):
        field = code.removeprefix("invalid_or_missing_")
        if field in {"job_category", "job_type", "role"}:
            provided = _field_value(row, field)
            return "Job validation", (
                f"Required {field.replace('_', ' ')} is missing."
                if provided == "missing" else
                f"{field.replace('_', ' ').capitalize()} {provided} is not a supported Advertio value."
            )
    if code in LOCATION_CODES:
        field = "province" if code == "unknown_province" else "city"
        return "Location validation", (
            JOB_REASON_TEXT[code] + f" Provided {field}: {_field_value(row, field)}."
        )
    if code in DATE_CODES:
        return "Source-date validation", JOB_REASON_TEXT[code]
    if code == "jobs_schema_changed":
        return "Advertio catalog", JOB_REASON_TEXT[code]
    if code.startswith("http_") and code[5:].isdigit():
        return "Advertio HTTP / catalog", (
            f"Advertio returned HTTP {code[5:]}; the exact endpoint and rejected field "
            "are not stored. Inspect sanitized system logs."
        )
    if code == "transport_or_catalog_error":
        return "Advertio HTTP / catalog", JOB_REASON_TEXT[code]
    if code == "unexpected_delivery_error":
        return "Delivery worker", JOB_REASON_TEXT[code]
    if code in JOB_REASON_TEXT:
        return "Job validation", JOB_REASON_TEXT[code]
    if status == "waiting":
        return "Delivery queue", "Queued but not yet assessed for publication."
    if status == "sending":
        return "Advertio delivery", "Delivery request is currently in progress."
    if code:
        return "Delivery", "A delivery issue was recorded; inspect the sanitized error code."
    return "Delivery", "No failure recorded for this delivery."


def _admission_diagnostic(row):
    reason = row["admission_reason"]
    if reason == "awaiting_jobs_extraction":
        state = str(row.get("ai_status") or "unset")
        code = str(row.get("ai_error") or "")
        if row.get("classification_status") == "failed":
            return "Classification", "Job classification failed before Jobs AI extraction."
        if state == "processed" and row.get("ai_category") != "joblist":
            return "AI extraction", "AI extraction completed for a different category, not Jobs."
        if state == "failed":
            return "AI extraction", AI_REASON_TEXT.get(
                code, "AI extraction failed; inspect the sanitized AI error code."
            )
        if state == "skipped":
            return "AI extraction", AI_REASON_TEXT.get(
                code, "AI extraction was skipped; inspect its recorded skip reason."
            )
        return "AI extraction", f"Awaiting successful Jobs AI extraction (state: {state[:25]})."
    return {
        "missing_extracted_record": (
            "AI persistence", "AI reports success, but no structured Jobs record was saved."
        ),
        "processing_incomplete": (
            "Text processing", "Text processing is not complete."
        ),
        "invalid_source_date": (
            "Source-date eligibility", "Original Telegram post date is missing or invalid."
        ),
        "future_source_date": (
            "Source-date eligibility", "Original Telegram post date is in the future."
        ),
        "outside_source_date_window": (
            "Source-date eligibility", "Original post is older than the configured Jobs freshness window."
        ),
        "requires_explicit_backfill": (
            "Queue admission", "This historical record requires explicit bounded Jobs backfill."
        ),
        "ready_for_queue": (
            "Queue admission", "Ready to enter the Jobs queue; validation and delivery have not run yet."
        ),
    }.get(reason, ("Queue admission", "No delivery attempt has been recorded."))


def _failure_diagnostics(conn):
    """Read-only stage/reason report. Select only metadata, never the listing body."""
    queue_rows = [
        dict(r) for r in conn.execute("""
            SELECT d.processed_message_id AS id, m.message_id, m.channel_username,
                   d.status, d.remote_status, d.error_code, d.updated_at,
                   j.role, j.job_category, j.job_type, j.province, j.city,
                   (SELECT a.http_status FROM advertio_jobs_attempts a
                    WHERE a.processed_message_id=d.processed_message_id
                    AND a.http_status IS NOT NULL ORDER BY a.id DESC LIMIT 1) AS http_status
            FROM advertio_jobs_deliveries d
            JOIN messages m ON m.id=d.processed_message_id
            LEFT JOIN joblist j ON j.processed_message_id=m.id
            WHERE d.status IN ('blocked','rejected','retry')
               OR (d.status IN ('sent','already_existed') AND d.remote_status='PendingReview')
            ORDER BY d.updated_at DESC LIMIT 50
        """)
    ]
    for item in queue_rows:
        item["stage"], item["reason"] = _queue_diagnostic(item)

    watermark = conn.execute(
        "SELECT value FROM advertio_jobs_meta WHERE key='deployment_watermark'"
    ).fetchone()
    boundary = int(watermark[0]) if watermark else 0
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(
        days=config.ADVERTIO_JOBS_FRESHNESS_DAYS
    )).isoformat()
    prequeue = [
        dict(r) for r in conn.execute("""
            WITH candidates AS (
                SELECT m.id, m.message_id, m.channel_username, m.ai_status, m.ai_error,
                       m.date AS source_date, m.classification_status, m.ai_category,
                       CASE
                        WHEN m.ai_status IS NOT 'processed' OR m.ai_category IS NOT 'joblist'
                         THEN 'awaiting_jobs_extraction'
                        WHEN j.processed_message_id IS NULL THEN 'missing_extracted_record'
                        WHEN m.processing_status IS NOT 'processed' THEN 'processing_incomplete'
                        WHEN julianday(m.date) IS NULL THEN 'invalid_source_date'
                        WHEN julianday(m.date)>julianday(?) THEN 'future_source_date'
                        WHEN julianday(m.date)<julianday(?) THEN 'outside_source_date_window'
                        WHEN m.id<=? THEN 'requires_explicit_backfill'
                        ELSE 'ready_for_queue'
                       END AS admission_reason
                FROM messages m
                LEFT JOIN joblist j ON j.processed_message_id=m.id
                LEFT JOIN advertio_jobs_deliveries d ON d.processed_message_id=m.id
                WHERE d.processed_message_id IS NULL
                  AND (m.classification_category='joblist' OR m.ai_category='joblist')
            )
            SELECT * FROM (
                SELECT candidates.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY admission_reason,
                            CASE WHEN admission_reason='awaiting_jobs_extraction'
                                 THEN ai_status ELSE '' END
                        ORDER BY id DESC
                    ) AS sample_rank
                FROM candidates
            )
            WHERE sample_rank<=8 ORDER BY admission_reason, id DESC LIMIT 50
        """, (now.isoformat(), cutoff, boundary))
    ]
    for item in prequeue:
        item["stage"], item["reason"] = _admission_diagnostic(item)

    grouped_queue = conn.execute("""
        SELECT status, remote_status, error_code, COUNT(*) AS n
        FROM advertio_jobs_deliveries
        WHERE status IN ('blocked','rejected','retry')
           OR (status IN ('sent','already_existed') AND remote_status='PendingReview')
        GROUP BY status, remote_status, error_code
    """).fetchall()
    grouped_ai = conn.execute("""
        SELECT m.ai_status, m.ai_error, COUNT(*) AS n
        FROM messages m
        LEFT JOIN advertio_jobs_deliveries d ON d.processed_message_id=m.id
        WHERE d.processed_message_id IS NULL
          AND (m.ai_category='joblist' OR m.classification_category='joblist')
          AND m.ai_status IN ('failed','skipped')
        GROUP BY m.ai_status, m.ai_error
    """).fetchall()
    summary = []
    for r in grouped_queue:
        status, remote, code, count = r
        stage, reason = _queue_diagnostic({
            "status": status, "remote_status": remote, "error_code": code
        })
        summary.append(dict(stage=stage, reason=reason, count=count, code=code or "none"))
    for r in grouped_ai:
        state, code, count = r
        stage, reason = _admission_diagnostic({
            "admission_reason": "awaiting_jobs_extraction",
            "ai_status": state, "ai_error": code,
        })
        summary.append(dict(stage=stage, reason=reason, count=count, code=code or "none"))
    summary.sort(key=lambda item: item["count"], reverse=True)
    return queue_rows, prequeue, summary[:30]



def jobs_report(conn):
    report = dict(
        enabled=bool(config.ADVERTIO_INGEST_ENABLED and config.ADVERTIO_JOBS_ENABLED),
        auto_publish=bool(config.ADVERTIO_JOBS_AUTO_PUBLISH),
        key_configured=bool(config.ADVERTIO_INGEST_KEY),
        processed=0,
        ai_failed=0,
        locally_eligible=0,
        blocked=0,
        waiting=0,
        sending=0,
        retry=0,
        rejected=0,
        sent=0,
        already_existed=0,
        active=0,
        pending_review=0,
        requests_today=0,
        remaining_budget=config.ADVERTIO_JOBS_DAILY_LIMIT,
        paused_until=None,
        pause_reason=None,
        recent=[],
        attempts=[],
        unassessed=0,
        delivery_issues=[],
        prequeue_issues=[],
        issue_summary=[],
    )
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "messages" in tables:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        if {"ai_status", "ai_category", "classification_category"} <= columns:
            report["ai_failed"] = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE ai_status='failed' AND classification_category='joblist'"
            ).fetchone()[0]
            report["processed"] = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE ai_status='processed' AND ai_category='joblist'"
            ).fetchone()[0]
    if "advertio_jobs_deliveries" not in tables:
        return report
    report["delivery_issues"], report["prequeue_issues"], report["issue_summary"] = _failure_diagnostics(conn)
    report["discovery"] = discovery_report(conn)
    report["backfill_discovery"] = discovery_report(conn, backfill=True)
    for row in conn.execute(
        "SELECT status,COUNT(*) n FROM advertio_jobs_deliveries GROUP BY status"
    ):
        report[row["status"]] = row["n"]
    for row in conn.execute(
        "SELECT remote_status,COUNT(*) n FROM advertio_jobs_deliveries GROUP BY remote_status"
    ):
        if row["remote_status"] == "Active":
            report["active"] = row["n"]
        if row["remote_status"] == "PendingReview":
            report["pending_review"] = row["n"]
    report["locally_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM advertio_jobs_deliveries WHERE validated_at IS NOT NULL AND safety_flags='[]' AND status NOT IN ('blocked','rejected')"
    ).fetchone()[0]
    report["unassessed"] = conn.execute(
        "SELECT COUNT(*) FROM advertio_jobs_deliveries WHERE validated_at IS NULL AND status IN ('waiting','retry')"
    ).fetchone()[0]
    day = datetime.now(timezone.utc).date().isoformat()
    posts = conn.execute(
        "SELECT COUNT(*) FROM advertio_jobs_attempts WHERE attempted_at>=?", (day,)
    ).fetchone()[0]
    jobs = conn.execute(
        "SELECT COUNT(*) FROM advertio_request_log WHERE category='jobs' AND attempted_at>=?",
        (day,),
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM advertio_request_log WHERE attempted_at>=?", (day,)
    ).fetchone()[0]
    report["requests_today"] = max(jobs, posts)
    report["remaining_budget"] = max(
        0,
        min(
            config.ADVERTIO_JOBS_DAILY_LIMIT - report["requests_today"],
            config.ADVERTIO_DAILY_REQUEST_LIMIT - total,
        ),
    )
    meta = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key,value FROM advertio_jobs_meta WHERE key IN ('paused_until','pause_reason','worker_until')"
        )
    }
    report.update(meta)
    report["recent"] = [
        dict(r)
        for r in conn.execute(
            """SELECT processed_message_id,status,remote_status,lead_id,error_code,attempts,updated_at,safety_flags FROM advertio_jobs_deliveries ORDER BY updated_at DESC LIMIT 30"""
        )
    ]
    report["attempts"] = [
        dict(r)
        for r in conn.execute(
            """SELECT processed_message_id,attempted_at,status,http_status,remote_status,error_code FROM advertio_jobs_attempts ORDER BY id DESC LIMIT 30"""
        )
    ]
    return report


def jobs_section(report, action_form, state_box):
    def e(value):
        return escape(str(value if value is not None else "—"))

    discovery = report.get("discovery") or {}
    backfill = report.get("backfill_discovery") or {}
    labels = {
        "awaiting_jobs_extraction": "Awaiting successful Jobs AI extraction",
        "missing_extracted_record": "AI marked successful but Jobs data missing",
        "processing_incomplete": "Text processing incomplete",
        "already_in_delivery_queue": "Already queued (check statuses below)",
        "invalid_source_date": "Missing / invalid original Telegram date",
        "future_source_date": "Original Telegram date is in the future",
        "outside_source_date_window": "Older than the permitted source-date window",
        "requires_explicit_backfill": "Historical records requiring explicit backfill",
        "ready_for_queue": "Ready for queue admission (not yet validated for publication)",
    }
    admission_rows = "".join(
        "<tr><td>" + e(label) + "</td><td>" + e((discovery.get("counts") or {}).get(key, 0))
        + "</td><td>" + e((backfill.get("counts") or {}).get(key, 0)) + "</td></tr>"
        for key, label in labels.items()
    )
    awaiting_summary = " · ".join(e(key) + ": " + e(count)
                                  for key, count in (discovery.get("awaiting_ai_states") or {}).items())
    admission_html = f"""<details class="ops-recovery" open><summary>Why are Jobs not being sent?</summary>
    <p>Awaiting AI breakdown: {awaiting_summary or "none"}</p>
    <p>Automatic window: {e(discovery.get('effective_days'))} days · Backfill window:
    {e(backfill.get('effective_days'))} days · Backfill: {'enabled' if backfill.get('backfill_enabled') else 'disabled'}.</p>
    <p class="muted">Windows use the original Telegram post date, not today's AI processing date.
    Backfill uses the smaller of BACKFILL_DAYS and FRESHNESS_DAYS. Processing old ads with AI does not automatically send them.
    Backfill counts below are a preview; enabling backfill alone does not start sending. Both modes exclude existing queue records.</p>
    <div class="scroll"><table><thead><tr><th>Admission reason</th><th>Normal send</th><th>Explicit backfill</th></tr></thead><tbody>{admission_rows}</tbody></table></div>
    <p class="muted">If everything is awaiting AI, first run extraction. If records need explicit backfill,
    verify the jobs are still open, then enable and run bounded backfill below. For old or invalid dates, verify source freshness before changing settings.
    Queued blocked/rejected records require correction and explicit requeue. A cooldown or depleted budget also prevents transmission.</p></details>"""

    missing_count = (discovery.get("counts") or {}).get("missing_extracted_record", 0)
    admission_html += action_form(
        "requeue_missing_jobs_extractions",
        f"Recover missing Jobs extraction data ({missing_count})",
        extra='<label>Batch size<input type="number" name="limit" min="1" max="1000" value="100"></label>',
        disabled=not missing_count or not config.is_ai_extraction_enabled("joblist"),
        confirm="Requeue only Jobs marked successful but missing stored extraction data? Existing Jobs data and delivery records are excluded. Run AI Extraction afterwards; this does not publish anything.",
    )

    ready = report.get("enabled") and report.get("key_configured")
    controls = action_form(
        "run_advertio_jobs",
        "Send next eligible Jobs",
        extra='<label>Batch size<input type="number" name="limit" min="1" max="100" value="5"></label>',
        disabled=not ready,
    )
    controls += action_form("stop_advertio_jobs", "Stop Jobs Delivery", danger=True)
    controls += '<details class="ops-recovery"><summary>Recovery &amp; historical backfill</summary><p class="muted">Backfill selects recent historical Jobs; AI extraction alone does not enqueue old listings.</p>'
    controls += action_form(
        "resume_advertio_jobs",
        "Resume after correcting API configuration",
        confirm="Acknowledge that the authentication, endpoint or schema fault has been corrected? Cooldowns will be cleared; all validation still applies.",
    )
    if config.ADVERTIO_JOBS_BACKFILL_ENABLED:
        controls += action_form(
            "backfill_advertio_jobs",
            "Explicit bounded Jobs backfill",
            extra='<label>Batch size<input type="number" name="limit" min="1" max="100" value="5"></label>',
            disabled=not ready,
            confirm="Recheck source freshness and availability before backfilling these recent historical Jobs.",
        )
    controls += "</details>"
    counts = "".join(
        '<span>' + e(label) + ": <strong>" + e(report.get(key, 0)) + "</strong></span>"
        for label, key in [
            ("AI processed", "processed"),
            ("AI failed", "ai_failed"),
            ("Locally validated eligible", "locally_eligible"),
            ("Blocked/incomplete", "blocked"),
            ("Waiting", "waiting"),
            ("Sending", "sending"),
            ("Retry", "retry"),
            ("Rejected by API", "rejected"),
            ("Sent to Advertio", "sent"),
            ("Already existed", "already_existed"),
            ("Actual remote Active", "active"),
            ("Actual remote PendingReview", "pending_review"),
            ("Not yet assessed", "unassessed"),
        ]
    )
    issue_counts = "".join(
        "<tr><td>" + e(item["stage"]) + "</td><td>" + e(item["reason"])
        + "</td><td>" + e(item["code"]) + "</td><td>" + e(item["count"])
        + "</td></tr>" for item in report.get("issue_summary", [])
    )
    delivery_issue_rows = "".join(
        "<tr>" + "".join("<td>" + e(value) + "</td>" for value in (
            item.get("id"), item.get("channel_username"), item.get("message_id"),
            item.get("status"), item.get("stage"), item.get("reason"),
            item.get("error_code"), item.get("http_status"),
        )) + "</tr>" for item in report.get("delivery_issues", [])
    )
    prequeue_issue_rows = "".join(
        "<tr>" + "".join("<td>" + e(value) + "</td>" for value in (
            item.get("id"), item.get("channel_username"), item.get("message_id"),
            item.get("ai_status"), item.get("stage"), item.get("reason"),
            item.get("admission_reason"), item.get("ai_error"),
        )) + "</tr>" for item in report.get("prequeue_issues", [])
    )
    diagnostic_html = f"""<details class="ops-recovery" open>
    <summary>Jobs failure diagnostics · stages &amp; reasons (English)</summary>
    <p class="muted">Read-only diagnostics. A blocked record failed local checks before the API call.
    Rejected means a permanent HTTP failure; Retry means an intermittent failure.
    PendingReview means the Advertio API accepted the listing but has not made it Active.
    HTTP error bodies are intentionally not retained; do not assume which exact field caused HTTP 400.</p>
    <h3>Failure reasons by stage</h3>
    <div class="scroll"><table><thead><tr><th>Failed stage</th><th>Explanation (English)</th>
    <th>Stored code</th><th>Count</th></tr></thead>
    <tbody>{issue_counts or '<tr><td colspan="4">No failed or review records found.</td></tr>'}</tbody></table></div>
    <h3>Latest delivery problems · up to 50</h3>
    <div class="scroll"><table><thead><tr><th>Local ID</th><th>Source</th><th>Telegram ID</th>
    <th>Delivery status</th><th>Failed stage</th><th>Explanation (English)</th>
    <th>Stored error code</th><th>Last HTTP</th></tr></thead>
    <tbody>{delivery_issue_rows or '<tr><td colspan="8">No local blocks, API failures or pending-review outcomes yet.</td></tr>'}</tbody></table></div>
    <h3>Not yet in the Jobs delivery queue · 50 prioritized examples</h3>
    <p class="muted">Includes extraction failures, skipped extraction, missing Jobs details,
    date exclusions and historical records requiring manual backfill.
    These are not API rejections: no Jobs ingest request was attempted for them.</p>
    <div class="scroll"><table><thead><tr><th>Local ID</th><th>Source</th><th>Telegram ID</th>
    <th>AI status</th><th>Stopped stage</th><th>Explanation (English)</th>
    <th>Admission code</th><th>AI error code</th></tr></thead>
    <tbody>{prequeue_issue_rows or '<tr><td colspan="8">No unqueued Jobs records found.</td></tr>'}</tbody></table></div>
    </details>"""

    rows = []
    for r in report.get("recent", []):
        retry = (
            action_form(
                "requeue_advertio_jobs",
                "Requeue after correction",
                extra='<input type="hidden" name="message_row_id" value="'
                + e(r["processed_message_id"])
                + '">',
                confirm="Requeue only after correcting the cause? Validation, freshness and idempotency remain mandatory.",
            )
            if r["status"] in ("blocked", "rejected")
            else ""
        )
        rows.append(
            "<tr>"
            + "".join(
                "<td>" + e(r.get(k)) + "</td>"
                for k in (
                    "processed_message_id",
                    "status",
                    "remote_status",
                    "lead_id",
                    "error_code",
                    "safety_flags",
                    "attempts",
                    "updated_at",
                )
            )
            + "<td>"
            + retry
            + "</td></tr>"
        )
    history = "".join(
        "<tr>"
        + "".join(
            "<td>" + e(r.get(k)) + "</td>"
            for k in (
                "processed_message_id",
                "attempted_at",
                "status",
                "http_status",
                "remote_status",
                "error_code",
            )
        )
        + "</tr>"
        for r in report.get("attempts", [])
    )
    return f"""<section class="ops-section" id="advertio-jobs"><h2>Advertio Jobs</h2>
    <p>Feature: <strong>{"enabled" if report.get("enabled") else "disabled"}</strong> ·
    Auto-publish: <strong>{"enabled" if report.get("auto_publish") else "disabled"}</strong> ·
    API key: <strong>{"configured" if report.get("key_configured") else "not configured"}</strong></p>
    <div class="ops-result-metrics">{counts}</div><p>Requests today (including Jobs catalog calls): {e(report.get("requests_today"))} ·
    Remaining Jobs/shared request budget: {e(report.get("remaining_budget"))}</p>
    <p>Queue cooldown until (UTC): {e(report.get("paused_until"))} · Reason: {e(report.get("pause_reason"))}</p>
    <p class="muted">Sent to Advertio means API acceptance. Actual Active/PendingReview are recorded receiver statuses at submission time, not live polling. Locally eligible is the cached validation result; unassessed rows have not passed catalog checks yet. Historical backfill is opt-in. Diagnostics below show the local, AI and HTTP stages.</p>
    <p><a href="/settings">Jobs auto-publish and delivery settings</a></p>
    {admission_html}
    {diagnostic_html}
    {state_box}<div class="op-actions">{controls}</div>
    <details class="ops-recovery"><summary>Recent Jobs records · status, errors &amp; requeue</summary><div class="scroll"><table><thead><tr><th>Local ID</th><th>Delivery</th><th>Remote status</th><th>Lead ID</th><th>Error</th><th>Safety flags</th><th>Evaluations</th><th>Updated UTC</th><th>Action</th></tr></thead><tbody>{"".join(rows) or '<tr><td colspan="9">No Jobs delivery records yet.</td></tr>'}</tbody></table></div></details>
    <details><summary>View delivery history</summary><div class="scroll"><table><thead><tr><th>Local ID</th><th>Attempt UTC</th><th>Outcome</th><th>HTTP</th><th>Remote</th><th>Error</th></tr></thead><tbody>{history}</tbody></table></div></details></section>"""
