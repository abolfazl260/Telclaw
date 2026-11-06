# Manual single-message crawl

The Back Office includes a **Manual Crawl** tab at `/crawl/manual`. This
imports one existing Telegram post from a source configured in
`TELCLAW_CHANNELS_FILE` (default `channels.json`).

## How to use

1. Open Back Office using an authorized admin link and select **Manual Crawl**.
2. Choose the source channel and an authorized Telegram account that can read it.
3. Paste the numeric Telegram message ID or a public permalink
   (`https://t.me/channel_username/1234`) matching the chosen source.
4. Click **Crawl message** and read the saved/skipped/error status.

The operation calls Telethon `get_messages(entity, ids=message_id)`; it does
not iterate the channel, wait for the crawler's randomized delay, or change
scheduled crawler configuration. Only public username channels appear in the
selector; for a configured private source, a numeric ID can be used if its
username is resolvable by the connected account. Invite links and `t.me/c/`
permalinks are not accepted directly.

The normal crawler's source attribution, sender checks, ten-word minimum,
forward-origin restrictions, raw-text preservation, duplicate fingerprints,
and SQLite uniqueness all apply. Saved messages enter the existing processing
queue (`processing_status=pending`, `ai_status=waiting`), just like a
scheduled crawl. **This action does not run the AI queues or publish ads.**
Use the regular Operations page to run queued workers if needed.

The HTTP form is protected by the existing Back Office session and CSRF
middleware. Source channels and Telegram accounts are revalidated server-side;
links cannot select a different source than the configured channel. One manual
crawl runs at a time, and an audit event records admin ID, source, message ID,
result and counts without duplicating the message body.

Status interpretation:
- **Stored:** a new message entered the SQLite queue.
- **Duplicate/skipped:** the existing crawler filters rejected the post;
  existing records remain unchanged.
- **Not found:** Telegram returned no post for this channel and message ID.
- **Failed:** account/channel access or Telegram/database retrieval failed;
  inspect the server logs.
