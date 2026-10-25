"""Read-only Jobs diagnostics and operations markup. No remote calls on page load."""

from html import escape
from datetime import datetime, timezone

import config
from delivery.jobs_discovery import discovery_report


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
    <p class="muted">Sent to Advertio means API acceptance. Actual Active/PendingReview are recorded receiver statuses at submission time, not live polling. Locally eligible is the cached validation result; unassessed rows have not passed catalog checks yet. Empty source allowlist blocks publication. Historical backfill is opt-in.</p>
    <p><a href="/settings">Jobs auto-publish, source approval and delivery settings</a></p>
    {admission_html}
    {state_box}<div class="op-actions">{controls}</div>
    <details class="ops-recovery"><summary>Recent Jobs records · status, errors &amp; requeue</summary><div class="scroll"><table><thead><tr><th>Local ID</th><th>Delivery</th><th>Remote status</th><th>Lead ID</th><th>Error</th><th>Safety flags</th><th>Evaluations</th><th>Updated UTC</th><th>Action</th></tr></thead><tbody>{"".join(rows) or '<tr><td colspan="9">No Jobs delivery records yet.</td></tr>'}</tbody></table></div></details>
    <details><summary>View delivery history</summary><div class="scroll"><table><thead><tr><th>Local ID</th><th>Attempt UTC</th><th>Outcome</th><th>HTTP</th><th>Remote</th><th>Error</th></tr></thead><tbody>{history}</tbody></table></div></details></section>"""
