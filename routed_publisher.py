"""Publish AI-processed ads through admin-managed destination rules."""
import logging

import aiohttp

import config
import routing_rules
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

    async def publish_pending(self, limit=50, pairs=None, resend=False, requested_by=None):
        if routing_rules.is_rate_limited():
            return {"found": 0, "sent": 0, "failed": 0, "rejected": 0, "rate_limited": True}
        pairs = routing_rules.pending(limit) if pairs is None else pairs
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
