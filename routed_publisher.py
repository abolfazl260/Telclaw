"""Publish AI-processed ads through admin-managed destination rules."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

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
        today = datetime.now(ZoneInfo("Asia/Tehran")).date().isoformat()
        conn = get_connection()
        try:
            target = conn.execute(
                "SELECT * FROM publishing_targets WHERE chat_id=? AND enabled=1",
                ("@koolbar_international",),
            ).fetchone()
            if not target:
                return []
            rows = conn.execute(
                """SELECT t.*, m.channel_username, m.message_id, m.message_link,
                          m.sender_username, m.ai_category, m.ai_status,
                          m.processing_status, m.id AS message_row_id
                     FROM transferlist t
                     INNER JOIN messages m ON m.id=t.processed_message_id
                    WHERE m.processing_status='processed'
                      AND m.ai_status='processed'
                      AND m.ai_category='transferlist'
                      AND t.departure_date IS NOT NULL
                      AND date(substr(t.departure_date,1,10)) >= date(?)
                      AND NOT EXISTS (
                          SELECT 1 FROM publishing_deliveries d
                           WHERE d.message_id=m.id
                             AND d.target_id=?
                             AND d.status='sent'
                      )
                    ORDER BY t.id ASC
                    LIMIT ?""",
                (today, target["id"], int(limit)),
            ).fetchall()
            return [(dict(row), dict(target)) for row in rows]
        finally:
            conn.close()

    async def publish_pending(self, limit=50, pairs=None, resend=False, requested_by=None):
        if routing_rules.is_rate_limited():
            return {"found": 0, "sent": 0, "failed": 0, "rejected": 0, "rate_limited": True}
        if pairs is None:
            pairs = routing_rules.pending(limit)
            # The legacy Koolbar channel keeps its historical behavior outside
            # the generic Backoffice rule builder: transferlist only, processed
            # AI data, and departure date today or later (Tehran date).
            existing = {(record["message_row_id"], rule["target_id"]) for record, rule in pairs}
            for pair in self._koolbar_pairs(limit):
                key = (pair[0]["message_row_id"], pair[1]["id"])
                if key not in existing and len(pairs) < int(limit):
                    pairs.append(pair)
                    existing.add(key)
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
