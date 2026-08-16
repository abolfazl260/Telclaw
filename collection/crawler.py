"""Telegram collection orchestration.

This layer only knows how to read Telegram messages and pass cleaned records
into the persistence boundary. AI, delivery, and console UI are intentionally
outside the collector.
"""

import asyncio
import random

from colorama import Fore, init
from telethon import errors

from processing.cleaner import clean_text, is_collectable_text
from processing.normalizer import normalize_channel_username, normalize_date
from storage import database

init(autoreset=True)


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


def _extract_media(message):
    has_media = message.media is not None
    file_unique_id = None
    media_type = None
    if not message.media:
        return has_media, media_type, file_unique_id
    if getattr(message.media, "document", None):
        file_unique_id = getattr(message.media.document, "id", None)
        media_type = "document"
    elif getattr(message.media, "photo", None):
        file_unique_id = getattr(message.media.photo, "id", None)
        media_type = "photo"
    return has_media, media_type, file_unique_id


async def crawl_channel(client, channel_username, target_date):
    channel_username = normalize_channel_username(channel_username)
    print(f"\n{Fore.CYAN}{'=' * 50}")
    print(f"📡 STARTING CRAWL: {Fore.YELLOW}{channel_username}")
    print(f"{Fore.CYAN}{'=' * 50}")

    saved_count = 0
    skipped_count = 0
    try:
        entity = await client.get_input_entity(channel_username)
        await asyncio.sleep(random.randint(30, 60))

        async for message in client.iter_messages(entity):
            if not message:
                skipped_count += 1
                continue

            message_text = clean_text(message.text)
            if not is_collectable_text(message_text):
                skipped_count += 1
                continue

            msg_date = message.date.date()
            print(
                f"🔗 ID: {message.id} | Date: {msg_date} | "
                f"Text: {message_text[:40]}"
            )
            if msg_date < target_date:
                print(f"\n🏁 Reached target date {target_date}. Stopping channel.")
                break

            has_media, media_type, file_unique_id = _extract_media(message)
            sender_id, sender_username, sender_type = _extract_sender(message)

            try:
                saved = database.insert_message(
                    channel_username=channel_username,
                    message_id=message.id,
                    text=message_text,
                    date_str=normalize_date(msg_date),
                    channel_id=getattr(entity, "channel_id", None),
                    channel_name=getattr(entity, "title", None),
                    sender_id=sender_id,
                    sender_username=sender_username,
                    sender_type=sender_type,
                    has_media=has_media,
                    media_type=media_type,
                    file_unique_id=file_unique_id,
                )
                if saved:
                    saved_count += 1
                else:
                    skipped_count += 1
                    print(f"SKIPPED: {message.id}")
            except Exception as exc:
                skipped_count += 1
                print(f"⚠️ Storage error: {exc}")

            await asyncio.sleep(random.uniform(0.3, 1.0))

        print(f"\n📊 CHANNEL RESULT: {channel_username}")
        print(f"✅ Saved: {saved_count}")
        print(f"⏭ Skipped: {skipped_count}")
    except errors.ChannelInvalidError:
        print(f"\n❌ Invalid channel: {channel_username}")
    except errors.ChannelPrivateError:
        print(f"\n❌ Private channel access denied: {channel_username}")
    except Exception as exc:
        print(f"\n❌ Crawl error ({channel_username}): {exc}")


async def start_crawler(client, channel_username, target_date):
    return await crawl_channel(client, channel_username, target_date)
