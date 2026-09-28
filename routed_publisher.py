"""Publish AI-processed ads through admin-managed destination rules."""
import logging

import aiohttp

import config
import routing_rules
from delivery.telegram_transfer_publisher import TelegramTransferPublisher, TransferTelegramPublishError

logger = logging.getLogger(__name__)


def _plain_ad(record):
    category = record["ai_category"]
    if category == "housinglist":
        fields = ("title", "description", "city", "province", "price", "currency")
    elif category == "joblist":
        fields = ("job_title", "company", "location", "salary", "salary_currency", "description")
    else:
        raise ValueError("Unsupported category")
    return "\n".join(str(record[field]).strip() for field in fields if record.get(field) is not None and str(record[field]).strip())[:4000]


class RoutedPublisher:
    def __init__(self, token=None):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        if not self.token:
            raise RuntimeError("TELCLAW_TELEGRAM_BOT_TOKEN is required")
        routing_rules.initialize()

    async def publish_pending(self, limit=50):
        pairs = routing_rules.pending(limit)
        result = {"found": len(pairs), "sent": 0, "failed": 0, "rejected": 0}
        blocked_targets = {}
        for record, rule in pairs:
            message_id, target_id = record["message_row_id"], rule["target_id"]
            if target_id in blocked_targets:
                routing_rules.record_delivery(message_id, target_id, "rejected",
                                              error=blocked_targets[target_id][:1000])
                result["rejected"] += 1
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
                            if response.status == 403:
                                error = f"Telegram sendMessage HTTP 403: {body.get('description', '')}"
                                blocked_targets[target_id] = error
                                routing_rules.update_target_connection(target_id, "disconnected", error)
                                routing_rules.record_delivery(message_id, target_id, "rejected", error=error[:1000])
                                result["rejected"] += 1
                                logger.warning("Publishing destination %s unavailable: %s", target_id, error)
                                continue
                            raise RuntimeError(f"Telegram sendMessage HTTP {response.status}: {body.get('description', '')}")
                routing_rules.record_delivery(message_id, target_id, "sent",
                                              body["result"].get("message_id"))
                result["sent"] += 1
            except (ValueError, TransferTelegramPublishError) as exc:
                routing_rules.record_delivery(message_id, target_id, "rejected", error=str(exc)[:1000])
                result["rejected"] += 1
            except Exception as exc:
                routing_rules.record_delivery(message_id, target_id, "retry", error=str(exc)[:1000])
                result["failed"] += 1
                logger.exception("Publication failed for message=%s target=%s", message_id, target_id)
        return result
