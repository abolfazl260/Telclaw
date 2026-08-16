
import asyncio
import random
from telethon import errors
from colorama import Fore, Style, init

import csv_storage

init(autoreset=True)


async def crawl_channel(client, channel_username, target_date):
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

            message_text = (message.text or "").strip()

            if not message_text:
                skipped_count += 1
                continue

            if len(message_text) < 20:
                skipped_count += 1
                continue

            msg_date = message.date.date()
            message_text = message.text or ""

            print(
                f"🔗 ID: {message.id} | "
                f"Date: {msg_date} | "
                f"Text: {message_text[:40]}"
            )

            if msg_date < target_date:
                print(f"\n🏁 Reached target date {target_date}. Stopping channel.")
                break

            has_media = message.media is not None
            file_unique_id = None
            media_type = None

            if message.media:
                if hasattr(message.media, "document") and message.media.document:
                    file_unique_id = getattr(message.media.document, "id", None)
                    media_type = "document"

                elif hasattr(message.media, "photo") and message.media.photo:
                    file_unique_id = message.media.photo.id
                    media_type = "photo"

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

            unique_message_key = f"{channel_username}_{message.id}"

            try:
                saved = csv_storage.save_message(
                    message_id=message.id,
                    channel_id=getattr(entity, "channel_id", None),
                    channel_username=channel_username,
                    sender_id=sender_id,
                    sender_username=sender_username,
                    sender_type=sender_type,
                    text=message_text,
                    has_media=has_media,
                    file_unique_id=file_unique_id,
                    media_type=media_type,
                    date=str(msg_date),
                    unique_key=unique_message_key
                )

                if saved:
                    saved_count += 1
                else:
                    skipped_count += 1
                    print(f"SKIPPED: {message.id}")

            except Exception as e:
                skipped_count += 1
                print(f"⚠️ CSV save error: {e}")

            await asyncio.sleep(random.uniform(0.3, 1.0))

        print(f"\n📊 CHANNEL RESULT: {channel_username}")
        print(f"✅ Saved: {saved_count}")
        print(f"⏭ Skipped: {skipped_count}")

    except errors.ChannelInvalidError:
        print(f"\n❌ Invalid channel: {channel_username}")

    except errors.ChannelPrivateError:
        print(f"\n❌ Private channel access denied: {channel_username}")

    except Exception as e:
        print(f"\n❌ Crawl error ({channel_username}): {e}")


async def start_crawler(client, channel_username, target_date):
    try:
        return await crawl_channel(client, channel_username, target_date)
    except Exception as e:
        print(f"\n❌ Fatal crawler error: {e}")
