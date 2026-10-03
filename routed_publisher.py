"""Publish AI-processed ads through admin-managed destination rules."""
import logging
from datetime import date

import aiohttp

import config
import routing_rules
from storage.database import get_connection
from delivery.telegram_transfer_publisher import TelegramTransferPublisher, TransferTelegramPublishError

logger = logging.getLogger(__name__)


def _plain_ad(record):
    """Format any structured non-transfer category without topic-specific branching."""
    category = record.get("ai_category")
    if category not in routing_rules.categories():
        raise ValueError("Unsupported category")
    fields = routing_rules.category_fields(category)
    preferred = ("title", "job_title", "name", "description", "company", "location", "city",
                 "province", "price", "currency", "salary", "salary_currency", "contact")
    ordered = [field for field in preferred if field in fields]
    ordered.extend(field for field in fields if field not in ordered)
    lines = []
    for field in ordered:
        value = record.get(field)
        if value is None or not str(value).strip():
            continue
        text = str(value).strip()
        if field in {"title", "job_title", "name", "description"}:
            lines.append(text)
        else:
            lines.append(f"{field.replace('_', ' ').title()}: {text}")
    if not lines:
        # Preserve the established cleaned -> raw fallback for non-transfer ads.
        # Do not substitute the legacy messages.text compatibility copy.
        fallback = str(record.get("cleaned_text") or record.get("raw_text") or "").strip()
        if fallback:
            lines.append(fallback)
    return "\n".join(lines)[:4000]


class RoutedPublisher:
    def __init__(self, token=None):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        if not self.token:
            raise RuntimeError("TELCLAW_TELEGRAM_BOT_TOKEN is required")
        routing_rules.initialize()
        # Keep the legacy Koolbar destination available without requiring a
        # Backoffice rule. Its publication eligibility is hard-coded below to
        # preserve the old channel behavior.
        if not any(target["chat_id"] == "@koolbar_international" for target in routing_rules.list_targets()):
            routing_rules.save_target(
                "Koolbar International",
                "@koolbar_international",
                description="Legacy hard-coded transfer destination",
            )
        # _claim() relies on the legacy publication table even when the
        # Backoffice test database has not initialized the legacy publisher.
        TelegramTransferPublisher._ensure_publication_table()

    @staticmethod
    def _koolbar_pairs(limit):
        """Return legacy Koolbar transfer ads with their former eligibility rules."""
        today = date.today().isoformat()
        # Use the routing module to resolve the target so its schema initialization
        # is guaranteed for isolated/test databases as well as the production DB.
        targets = [
            target for target in routing_rules.list_targets()
            if target["chat_id"] == "@koolbar_international" and target["enabled"]
        ]
        if not targets:
            return []
        target = targets[0]
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT t.*, m.channel_username, m.message_id, m.message_link,
                          m.sender_username, m.raw_text, m.ai_category, m.ai_status,
                          m.processing_status, m.id AS message_row_id
                     FROM transferlist t
                     INNER JOIN messages m ON m.id=t.processed_message_id
                    WHERE m.processing_status='processed'
                      AND m.ai_status='processed'
                      AND m.ai_category='transferlist'
                      AND t.departure_date IS NOT NULL
                      AND TRIM(COALESCE(t.origin_city, '')) <> ''
                      AND TRIM(COALESCE(t.destination_city, '')) <> ''
                      AND date(substr(t.departure_date,1,10)) >= date(?)
                      AND NOT EXISTS (
                          SELECT 1 FROM publishing_deliveries d
                           WHERE d.message_id=m.id
                             AND d.target_id=?
                             AND d.status IN ('sent','rejected','sending','uncertain')
                      )
                    ORDER BY t.id ASC
                    LIMIT ?""",
                (today, target["id"], int(limit)),
            ).fetchall()
            rule = dict(target)
            rule["target_id"] = target["id"]
            rule["target_enabled"] = target["enabled"]
            rule["category"] = "transferlist"
            return [(dict(row), rule) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def koolbar_diagnostics(limit=50):
        """Return read-only eligibility diagnostics for the legacy Koolbar destination."""
        today = date.today().isoformat()
        targets = [
            target for target in routing_rules.list_targets()
            if target["chat_id"] == "@koolbar_international"
        ]
        if not targets:
            return {
                "configured": False,
                "target_id": None,
                "target_enabled": False,
                "today": today,
                "eligible_count": 0,
                "rows": [],
            }

        target = targets[0]
        conn = get_connection()
        try:
            eligible_count = 0
            if target["enabled"]:
                eligible_count = int(conn.execute(
                    """SELECT COUNT(*)
                         FROM transferlist t
                         INNER JOIN messages m ON m.id=t.processed_message_id
                        WHERE m.processing_status='processed'
                          AND m.ai_status='processed'
                          AND m.ai_category='transferlist'
                          AND t.departure_date IS NOT NULL
                          AND TRIM(COALESCE(t.origin_city, '')) <> ''
                          AND TRIM(COALESCE(t.destination_city, '')) <> ''
                          AND date(substr(t.departure_date,1,10)) >= date(?)
                          AND NOT EXISTS (
                              SELECT 1 FROM publishing_deliveries d
                               WHERE d.message_id=m.id
                                 AND d.target_id=?
                                 AND d.status IN ('sent','rejected','sending','uncertain')
                          )""",
                    (today, target["id"]),
                ).fetchone()[0])

            rows = conn.execute(
                """SELECT m.id AS message_row_id, m.message_id AS telegram_source_id,
                          m.channel_username, m.processing_status, m.ai_status, m.ai_category,
                          t.origin_city, t.destination_city, t.departure_date,
                          d.status AS delivery_status, d.error AS delivery_error,
                          d.updated_at AS delivery_updated_at,
                          CASE WHEN m.processing_status='processed' THEN 1 ELSE 0 END AS processing_ok,
                          CASE WHEN m.ai_status='processed' THEN 1 ELSE 0 END AS ai_ok,
                          CASE WHEN m.ai_category='transferlist' THEN 1 ELSE 0 END AS category_ok,
                          CASE WHEN TRIM(COALESCE(t.origin_city, '')) <> ''
                                     AND TRIM(COALESCE(t.destination_city, '')) <> ''
                               THEN 1 ELSE 0 END AS route_ok,
                          CASE WHEN t.departure_date IS NOT NULL
                                     AND date(substr(t.departure_date,1,10)) >= date(?)
                               THEN 1 ELSE 0 END AS date_ok,
                          CASE WHEN d.status IS NULL
                                     OR d.status NOT IN ('sent','rejected','sending','uncertain')
                               THEN 1 ELSE 0 END AS delivery_ok
                     FROM transferlist t
                     INNER JOIN messages m ON m.id=t.processed_message_id
                     LEFT JOIN publishing_deliveries d
                       ON d.message_id=m.id AND d.target_id=?
                    ORDER BY t.id DESC
                    LIMIT ?""",
                (today, target["id"], min(max(int(limit), 1), 200)),
            ).fetchall()
        finally:
            conn.close()

        details = []
        for item in rows:
            row = dict(item)
            blockers = []
            if not target["enabled"]:
                blockers.append("target disabled")
            if not row["processing_ok"]:
                blockers.append("processing_status is not processed")
            if not row["ai_ok"]:
                blockers.append("ai_status is not processed")
            if not row["category_ok"]:
                blockers.append("ai_category is not transferlist")
            if not row["route_ok"]:
                blockers.append("origin or destination is empty")
            if not row["date_ok"]:
                blockers.append("departure date is missing, invalid, or in the past")
            if not row["delivery_ok"]:
                blockers.append(f"delivery status is {row['delivery_status']}")
            row["eligible"] = not blockers
            row["blockers"] = blockers
            details.append(row)

        return {
            "configured": True,
            "target_id": target["id"],
            "target_enabled": bool(target["enabled"]),
            "today": today,
            "eligible_count": eligible_count,
            "rows": details,
        }

    @staticmethod
    def _merge_pending_pairs(rule_pairs, koolbar_pairs, limit):
        """Fairly share one publishing cycle between managed rules and legacy Koolbar."""
        limit = max(0, int(limit))
        if limit == 0:
            return []

        rule_pairs = list(rule_pairs or [])
        koolbar_pairs = list(koolbar_pairs or [])
        merged = []
        seen = set()
        rule_index = koolbar_index = 0

        def append_unique(pair):
            record, rule = pair
            target_id = rule.get("target_id", rule.get("id"))
            key = (record["message_row_id"], target_id)
            if key in seen:
                return False
            seen.add(key)
            merged.append(pair)
            return True

        # Round-robin while both queues have work. This guarantees that a full
        # managed-rule queue can no longer consume every slot before Koolbar is
        # considered, while preserving ordering inside each queue.
        while len(merged) < limit and rule_index < len(rule_pairs) and koolbar_index < len(koolbar_pairs):
            append_unique(rule_pairs[rule_index])
            rule_index += 1
            if len(merged) >= limit:
                break
            append_unique(koolbar_pairs[koolbar_index])
            koolbar_index += 1

        # Let whichever queue still has work use the remaining capacity.
        while len(merged) < limit and rule_index < len(rule_pairs):
            append_unique(rule_pairs[rule_index])
            rule_index += 1
        while len(merged) < limit and koolbar_index < len(koolbar_pairs):
            append_unique(koolbar_pairs[koolbar_index])
            koolbar_index += 1

        return merged

    async def publish_pending(self, limit=50, pairs=None, resend=False, requested_by=None):
        if routing_rules.is_rate_limited():
            return {"found": 0, "sent": 0, "failed": 0, "rejected": 0, "rate_limited": True}
        if pairs is None:
            rule_pairs = routing_rules.pending(limit)
            # The legacy Koolbar channel keeps its historical eligibility rules,
            # but now shares the cycle fairly with managed publishing rules so a
            # full rule queue cannot starve it indefinitely.
            koolbar_pairs = self._koolbar_pairs(limit)
            pairs = self._merge_pending_pairs(rule_pairs, koolbar_pairs, limit)
        result = {"found": len(pairs), "sent": 0, "failed": 0, "rejected": 0}
        blocked_targets = {}
        for record, rule in pairs:
            message_id, target_id = record["message_row_id"], rule["target_id"]
            resend_id = None
            if resend:
                resend_id = routing_rules.claim_resend(message_id, target_id, requested_by)
                if resend_id is None:
                    continue

            def record_result(status, telegram_message_id=None, error=None):
                if resend:
                    routing_rules.record_resend(resend_id, status, telegram_message_id, error)
                else:
                    routing_rules.record_delivery(message_id, target_id, status,
                                                  telegram_message_id, error)

            if target_id in blocked_targets:
                record_result("rejected", error=blocked_targets[target_id][:1000])
                result["rejected"] += 1
                continue
            if not resend and not routing_rules.claim_delivery(message_id, target_id):
                continue
            try:
                if record["ai_category"] == "transferlist":
                    # Preserve the existing TR-XXXXXX numbering scheme used by the
                    # legacy transfer publisher. The number is message-scoped, so
                    # retries and multiple publishing targets keep the same ID.
                    record["ad_number"] = TelegramTransferPublisher._claim(
                        message_id, rule["chat_id"]
                    )
                    text = TelegramTransferPublisher.format_ad(record, record)
                    markup = TelegramTransferPublisher._contact_button(record)
                else:
                    text = _plain_ad(record)
                    markup = None
                if not text:
                    raise ValueError("Empty advertisement")
                payload = {"chat_id": rule["chat_id"], "text": text,
                           "disable_web_page_preview": True}
                if markup:
                    payload["reply_markup"] = markup
                timeout = aiohttp.ClientTimeout(total=config.TRANSFER_TELEGRAM_TIMEOUT_SECONDS)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                            json=payload) as response:
                        body = await response.json(content_type=None)
                        if not response.ok or not body.get("ok"):
                            if response.status == 429:
                                retry_after = (body.get("parameters") or {}).get("retry_after", 60)
                                try:
                                    seconds = min(max(int(retry_after), 1), 3600)
                                except (ValueError, TypeError):
                                    seconds = 60
                                routing_rules.set_rate_limit(seconds)
                                record_result("failed" if resend else "retry",
                                              error=f"Telegram rate limit: retry after {seconds}s")
                                result["failed"] += 1
                                result["rate_limited"] = True
                                logger.warning("Telegram rate limit reached; publishing paused for %ss", seconds)
                                return result
                            if response.status == 403:
                                error = f"Telegram sendMessage HTTP 403: {body.get('description', '')}"
                                blocked_targets[target_id] = error
                                routing_rules.update_target_connection(target_id, "disconnected", error)
                                record_result("rejected", error=error[:1000])
                                result["rejected"] += 1
                                logger.warning("Publishing destination %s unavailable: %s", target_id, error)
                                continue
                            raise RuntimeError(f"Telegram sendMessage HTTP {response.status}: {body.get('description', '')}")
                record_result("sent", body["result"].get("message_id"))
                result["sent"] += 1
            except (ValueError, TransferTelegramPublishError) as exc:
                record_result("rejected", error=str(exc)[:1000])
                result["rejected"] += 1
            except Exception as exc:
                record_result("failed" if resend else "retry", error=str(exc)[:1000])
                result["failed"] += 1
                logger.exception("Publication failed for message=%s target=%s", message_id, target_id)
        return result
