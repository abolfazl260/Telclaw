"""Read-only Jobs diagnostics and operations markup. No remote calls on page load."""

from html import escape
from datetime import datetime, timezone

import config


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

    ready = report.get("enabled") and report.get("key_configured")
    controls = action_form(
        "run_advertio_jobs",
        "Send next eligible Jobs",
        extra='<label>Batch size<input type="number" name="limit" min="1" max="100" value="5"></label>',
        disabled=not ready,
    )
    controls += action_form("stop_advertio_jobs", "Stop Jobs Delivery", danger=True)
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
    counts = " · ".join(
        e(label) + ": <strong>" + e(report.get(key, 0)) + "</strong>"
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
    <p>{counts}</p><p>Requests today (including Jobs catalog calls): {e(report.get("requests_today"))} ·
    Remaining Jobs/shared request budget: {e(report.get("remaining_budget"))}</p>
    <p>Queue cooldown until (UTC): {e(report.get("paused_until"))} · Reason: {e(report.get("pause_reason"))}</p>
    <p class="muted">Sent to Advertio means API acceptance. Actual Active/PendingReview are recorded receiver statuses at submission time, not live polling. Locally eligible is the cached validation result; unassessed rows have not passed catalog checks yet. Empty source allowlist blocks publication. Historical backfill is opt-in.</p>
    <p><a href="/settings">Jobs auto-publish, source approval and delivery settings</a></p>
    {state_box}<div class="op-actions">{controls}</div>
    <div class="scroll"><table><thead><tr><th>Local ID</th><th>Delivery</th><th>Remote status</th><th>Lead ID</th><th>Error</th><th>Safety flags</th><th>Evaluations</th><th>Updated UTC</th><th>Action</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
    <details><summary>View delivery history</summary><div class="scroll"><table><thead><tr><th>Local ID</th><th>Attempt UTC</th><th>Outcome</th><th>HTTP</th><th>Remote</th><th>Error</th></tr></thead><tbody>{history}</tbody></table></div></details></section>"""
