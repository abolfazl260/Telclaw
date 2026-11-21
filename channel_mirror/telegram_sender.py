"""Non-forwarding media transport with a durable album caption checkpoint."""

import asyncio

from .bot_api import APIError
from .models import SendOutcome, ValidationError, caption
from .source import Source


class Sender:
    def __init__(self, repo, api, source_factory=Source):
        self.repo, self.api, self.source_factory = repo, api, source_factory
        self.bot_id = None

    async def check_target(self, job):
        # Validate the frozen public handle; never invent one from a numeric ID.
        caption(job["destination_username"], "")
        if len(job["caption"].encode('utf-16-le')) // 2 > 1024:
            raise ValidationError("Caption exceeds Telegram limits")
        if self.bot_id is None:
            me = await self.api.call("getMe", {})
            self.bot_id = me["id"]
        chat = await self.api.call("getChat", {"chat_id": job["destination_chat_id"]})
        if (chat.get("id") != job["destination_chat_id"] or chat.get("type") != "channel"
                or ("@" + str(chat.get("username", ""))).casefold() != job["destination_username"].casefold()):
            raise ValidationError("Destination must retain its configured public @username and chat identity")
        member = await self.api.call("getChatMember", {"chat_id": job["destination_chat_id"], "user_id": self.bot_id})
        if member.get("status") not in {"creator", "administrator"} or (
                member.get("status") == "administrator" and member.get("can_post_messages") is not True):
            raise ValidationError("Bot lacks destination can_post_messages permission")

    async def _edit_caption(self, job, ids):
        try:
            await self.api.call("editMessageCaption", {"chat_id": job["destination_chat_id"],
                "message_id": ids[0], "caption": job["caption"], "caption_entities": [],
                "reply_markup": {"inline_keyboard": []}}, guard=lambda: self.repo.claim_sendable(job))
        except APIError as exc:
            if exc.code != 400 or "message is not modified" not in str(exc).lower():
                raise
        return SendOutcome("sent", tuple(ids))

    @staticmethod
    def _ids(result, album=False):
        values = result if album else [result]
        if not isinstance(values, list):
            raise APIError("Missing destination message IDs", uncertain=True)
        ids = []
        for item in values:
            mid = item.get("message_id") if isinstance(item, dict) else None
            if type(mid) is not int or mid <= 0 or mid in ids:
                raise APIError("Invalid destination message IDs", uncertain=True, message_ids=tuple(ids))
            ids.append(mid)
        return ids

    async def send(self, job, client):
        try:
            await self.check_target(job)
            if not self.repo.claim_sendable(job):
                return SendOutcome("retry", error="Rule is no longer active", retry_after=30)
            if job["publish_stage"] == "partial":
                return SendOutcome("uncertain", error="Partial album requires manual verification; refusing to copy again")
            if job["publish_stage"] == "caption_pending":
                return await self._edit_caption(job, job["destination_message_ids"])
            source = self.source_factory(client)
            inspected = await source.inspect(job)
            album = len(job["items"]) > 1
            fast = True
            try:
                origin = await self.api.call("getChat", {"chat_id": job["source_peer_id"]})
                if origin.get("has_protected_content"):
                    raise ValidationError("Source content is protected")
                if origin.get("id") != job["source_peer_id"]:
                    raise ValidationError("Source identity mismatch")
            except APIError as exc:
                if exc.code not in {400, 403} or "protect" in str(exc).lower():
                    raise
                fast = False
            # Albums with inline keyboards use the sanitized upload path;
            # copyMessages has no reply_markup override parameter.
            if album and any(getattr(m, "reply_markup", None) for m, _ in inspected):
                fast = False
            if fast:
                data = {"chat_id": job["destination_chat_id"], "from_chat_id": job["source_peer_id"]}
                if album:
                    method = "copyMessages"
                    data.update(message_ids=[i["message_id"] for i in job["items"]], remove_caption=True)
                else:
                    method = "copyMessage"
                    data.update(message_id=job["items"][0]["message_id"], caption=job["caption"],
                                caption_entities=[], reply_markup={"inline_keyboard": []})
                try:
                    result = await self.api.call(method, data, guard=lambda: self.repo.claim_sendable(job))
                except APIError as exc:
                    unavailable = any(s in str(exc).lower() for s in ("message to copy not found", "chat not found", "not a member"))
                    if exc.code not in {400, 403} or exc.uncertain or not unavailable:
                        raise
                    fast = False
            if not fast:
                async with source.download(inspected) as files:
                    data = {"chat_id": job["destination_chat_id"]}
                    if album:
                        method = "sendMediaGroup"
                        data["media"] = [{"type": i["media_kind"], "media": "attach://" + files[n][0],
                            "caption": job["caption"] if n == 0 else "", "caption_entities": []}
                            for n, i in enumerate(job["items"])]
                    else:
                        kind = job["items"][0]["media_kind"]
                        method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[kind]
                        data.update(caption=job["caption"], caption_entities=[], reply_markup={"inline_keyboard": []})
                        # The attachment field of a standalone upload is its media kind.
                        files = [(kind, files[0][1], files[0][2])]
                        if kind == "document":
                            data["disable_content_type_detection"] = True
                    result = await self.api.call(method, data, files=files, guard=lambda: self.repo.claim_sendable(job))
            ids = self._ids(result, album)
            if album:
                if not self.repo.checkpoint_media(job, ids):
                    return SendOutcome("uncertain", tuple(ids), "Publication checkpoint could not be fenced")
                if len(ids) != len(job["items"]):
                    return SendOutcome("uncertain", tuple(ids), "Only part of the source album was copied")
                if fast:
                    return await self._edit_caption(job, ids)
            return SendOutcome("sent", tuple(ids))
        except APIError as exc:
            status = "uncertain" if exc.uncertain else "retry" if exc.code == 0 or exc.code in {409, 429} or exc.code >= 500 else "failed"
            return SendOutcome(status, message_ids=exc.message_ids, error=str(exc), retry_after=exc.retry_after)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            return SendOutcome("failed", error=str(exc)[:1000])
        except (ConnectionError, OSError, asyncio.TimeoutError):
            # Telethon read/download failed before a Bot API mutation.
            return SendOutcome("retry", error="Source read/download temporarily unavailable")
