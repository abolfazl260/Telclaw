"""Bounded, explicit Telegram ID lookups inside a successful source crawl."""

import asyncio
import logging

from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.types import InputMessageID, MessageEmpty

from delivery.advertio_lifecycle import deletion_candidates, record_source_check

log = logging.getLogger(__name__)


async def check_source_removals(client, entity, anchor):
    channel_id = getattr(entity, "channel_id", None)
    if not channel_id or not anchor:
        return
    try:
        candidates = await asyncio.to_thread(deletion_candidates, channel_id)
        ids = sorted(set(candidates) | {anchor})
        if not candidates:
            return
        found = await asyncio.wait_for(client(GetMessagesRequest(
            entity, [InputMessageID(id=mid) for mid in ids]
        )), timeout=15)
        messages = getattr(found, "messages", None)
        if not isinstance(messages, list) or len(messages) != len(ids):
            return
        by_id = {getattr(message, "id", None): message for message in messages}
        if set(by_id) != set(ids):
            return

        def same_channel(message):
            return getattr(getattr(message, "peer_id", None), "channel_id", None) == channel_id

        if isinstance(by_id[anchor], MessageEmpty) or not same_channel(by_id[anchor]):
            return
        present = [mid for mid in candidates
                   if not isinstance(by_id[mid], MessageEmpty) and same_channel(by_id[mid])]
        absent = [mid for mid in candidates if isinstance(by_id[mid], MessageEmpty)
                  and (getattr(by_id[mid], "peer_id", None) is None or same_channel(by_id[mid]))]
        await asyncio.to_thread(record_source_check, channel_id, present, absent)
    except Exception as exc:
        # No state is inferred from a private channel, timeout, or Telegram
        # error. Keep normal collection results independent of this check.
        log.warning("Advertio removal lookup unavailable for Telegram channel %s (%s)",
                    channel_id, type(exc).__name__)
