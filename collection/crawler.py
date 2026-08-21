"""Telegram collection only.

This module is intentionally limited to Telegram I/O and collection metadata.
Cleaning, classification and property extraction belong to processing.
"""

import asyncio
import random

from colorama import Fore, init
from telethon import errors

from processing.normalizer import normalize_channel_username, normalize_date
from services.message_service import MessageService

init(autoreset=True)

COLLECTION_VERSION = "collection-v4"
CRAWL_MODE_ALL = "all"
CRAWL_MODE_PHOTOS_ONLY = "photos_only"
VALID_CRAWL_MODES = {CRAWL_MODE_ALL, CRAWL_MODE_PHOTOS_ONLY}


def _extract_sender(message):
    sender_type = "unknown"
    sender_id = None
    sender_username = None
    if message.sender:
        sender_id = getattr(message.sender, "id", None)
        sender_username = getattr(message.sender, "username", None)
        if getattr(message.sender, "bot", False):
            sender_type = "bot"
        elif getattr(message.sender, "broadcast", False):
            sender_type = "channel"
        else:
            sender_type = "user"
    return sender_id, sender_username, sender_type


def _extract_message_text(message):
    """Extract Telegram text/caption reliably, including media captions."""
    for attribute in ("text", "raw_text", "message"):
        value = getattr(message, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _build_message_link(channel_username, message_id):
    """Build a human-readable Telegram message link when a public username exists."""
    username = (channel_username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}/{message_id}"


def _extract_media(message, channel_username):
    has_media = message.media is not None
    file_unique_id = None
    media_type = None
    if not message.media:
        return has_media, media_type, file_unique_id, None, None

    if getattr(message.media, "document", None):
        file_unique_id = getattr(message.media.document, "id", None)
        media_type = "document"
    elif getattr(message.media, "photo", None):
        file_unique_id = getattr(message.media.photo, "id", None)
        media_type = "photo"

    message_link = _build_message_link(channel_username, message.id)
    media_reference = (
        f"telegram://{channel_username}/{message.id}"
        if channel_username
        else None
    )
    return has_media, media_type, file_unique_id, message_link, media_reference


def _should_collect(media_type, crawl_mode):
    if crawl_mode == CRAWL_MODE_ALL:
        return True
    return media_type == "photo"


def _log_extracted_message(channel_username, message, raw_text, media_type, message_link):
    """Show every collected message in the log before persistence/processing."""
    preview = raw_text.replace("\n", " ")
    print(
        f"\n📥 [EXTRACTED] channel={channel_username} "
        f"message_id={message.id} date={message.date.isoformat()} "
        f"media={media_type or 'none'}"
    )
    print(f"   text={preview}")
    if message_link:
        print(f"   link={message_link}")


async def crawl_channel(
    client,
    channel_username,
    from_date,
    to_date,
    crawl_mode=CRAWL_MODE_ALL,
):
    """Collect messages whose dates fall inclusively inside ``from_date..to_date``.

    Collection stores message and media metadata only. Physical media download
    is intentionally deferred until after AI validation and immediately before
    Advertio delivery.
    """
    channel_username = normalize_channel_username(channel_username)
    crawl_mode = (crawl_mode or CRAWL_MODE_ALL).strip().lower()
    if crawl_mode not in VALID_CRAWL_MODES:
        raise ValueError(
            f"Unsupported crawl mode: {crawl_mode}. "
            f"Use one of: {', '.join(sorted(VALID_CRAWL_MODES))}"
        )
    if from_date > to_date:
        raise ValueError("from_date cannot be later than to_date")

    print(f"\n{Fore.CYAN}{'=' * 50}")
    print(f"📡 STARTING CRAWL: {Fore.YELLOW}{channel_username}")
    print(f"📅 DATE RANGE: {Fore.YELLOW}{from_date} → {to_date}")
    print(f"🔎 MODE: {Fore.YELLOW}{crawl_mode}")
    print(f"👤 USERNAME REQUIRED: {Fore.YELLOW}yes")
    print(f"🖼️ MEDIA DOWNLOAD: {Fore.YELLOW}deferred until AI validation")
    print(f"{Fore.CYAN}{'=' * 50}")

    repository = MessageService()
    saved_count = 0
    skipped_count = 0
    filtered_count = 0
    bot_filtered_count = 0
    no_username_count = 0
    media_metadata_count = 0
    try:
        entity = await client.get_input_entity(channel_username)
        await asyncio.sleep(random.randint(30, 60))

        async for message in client.iter_messages(entity):
            if not message:
                skipped_count += 1
                continue

            msg_date = message.date.date()
            if msg_date > to_date:
                continue
            if msg_date < from_date:
                print(f"\n🏁 Reached start date {from_date}. Stopping channel.")
                break

            sender_id, sender_username, sender_type = _extract_sender(message)
            if sender_type == "bot":
                bot_filtered_count += 1
                print(
                    f"⏭ [BOT-SKIPPED] channel={channel_username} "
                    f"message_id={message.id}"
                )
                continue

            if not sender_username or not str(sender_username).strip():
                no_username_count += 1
                skipped_count += 1
                print(
                    f"⏭ [NO-USERNAME-SKIPPED] channel={channel_username} "
                    f"message_id={message.id} sender_id={sender_id} "
                    "reason=sender has no Telegram username"
                )
                continue

            raw_text = _extract_message_text(message)
            (
                has_media,
                media_type,
                file_unique_id,
                message_link,
                media_reference,
            ) = _extract_media(message, channel_username)

            if not _should_collect(media_type, crawl_mode):
                filtered_count += 1
                continue

            _log_extracted_message(
                channel_username, message, raw_text, media_type, message_link
            )

            # IMPORTANT: collection never downloads physical media anymore.
            # Preserve metadata required for the post-AI downloader instead.
            media_path = None
            if has_media:
                media_metadata_count += 1
                print(
                    f"   🖼️ [MEDIA-METADATA] type={media_type or 'unknown'} "
                    "download=deferred"
                )

            try:
                saved = repository.save_collected_message(
                    channel_username=channel_username,
                    message_id=message.id,
                    text=raw_text,
                    raw_text=raw_text,
                    cleaned_text=None,
                    date_str=normalize_date(msg_date),
                    collection_status="collected",
                    processing_status="pending",
                    ai_status="waiting",
                    pipeline_version=COLLECTION_VERSION,
                    cleaned_at=None,
                    channel_id=getattr(entity, "channel_id", None),
                    channel_name=getattr(entity, "title", None),
                    sender_id=sender_id,
                    sender_username=sender_username,
                    sender_type=sender_type,
                    has_media=has_media,
                    media_type=media_type,
                    file_unique_id=file_unique_id,
                    media_path=media_path,
                    message_link=message_link,
                    media_reference=media_reference,
                )
                if saved:
                    saved_count += 1
                    print("   💾 [RAW-SAVED] SQLite")
                else:
                    skipped_count += 1
                    print("   ⏭ [DUPLICATE] already stored")
            except Exception as exc:
                skipped_count += 1
                print(f"   ⚠️ [RAW-ERROR] Storage error: {exc}")

            await asyncio.sleep(random.uniform(0.3, 1.0))

        print(f"\n📊 CHANNEL RESULT: {channel_username}")
        print(f"📅 Range: {from_date} → {to_date}")
        print(f"✅ Saved: {saved_count}")
        print(f"🖼️ Media metadata saved: {media_metadata_count}")
        print("🖼️ Media downloaded during crawl: 0")
        print(f"🔍 Filtered by crawl mode: {filtered_count}")
        print(f"🤖 Bot messages skipped: {bot_filtered_count}")
        print(f"👤 No-username messages skipped: {no_username_count}")
        print(f"⏭ Skipped: {skipped_count}")
    except errors.ChannelInvalidError:
        print(f"\n❌ Invalid channel: {channel_username}")
    except errors.ChannelPrivateError:
        print(f"\n❌ Private channel access denied: {channel_username}")
    except Exception as exc:
        print(f"\n❌ Crawl error ({channel_username}): {exc}")


async def start_crawler(
    client,
    channel_username,
    from_date,
    to_date,
    crawl_mode=CRAWL_MODE_ALL,
):
    return await crawl_channel(
        client,
        channel_username,
        from_date,
        to_date,
        crawl_mode=crawl_mode,
    )
