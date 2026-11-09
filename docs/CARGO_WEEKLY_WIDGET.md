# Advertio Cargo weekly route report (Telegram Bot API 10.4)

## Delivery surfaces

Telegram **production** currently supports HTML widgets in **private bot chats only**:
https://core.telegram.org/bots/widgets

- The channel receives a regular Bot API HTML text post with an inline button to
  \`https://t.me/<actual_bot_username>?start=cargo_weekly\`.
- A user opens the bot and presses Start; \`/start cargo_weekly\` or the public
  \`/cargoreport\` command sends a real \`sendWidget\` message.
- No Mini App, web server, external scripts or widget network calls are needed.
- Unsupported/older clients can still read the widget's caption.
- Existing administrator-only monitoring commands and forward-import behavior
  are not exposed to regular users.

## Activation

1. Deploy with Telegram Bot API 10.4 compatibility and start the existing
   Telclaw monitoring bot; this is the bot users will open from the channel.
2. Make the bot an administrator of the destination channel with permission to
   publish messages.
3. Set \`TELCLAW_CARGO_WEEKLY_REPORT_CHANNEL=@advertio_cargo\` (or another
   channel ID/username), then set \`TELCLAW_CARGO_WEEKLY_REPORT_ENABLED=true\`.
4. Restart Telclaw. The publisher checks every 30 minutes and publishes at
   most one report per completed Tehran week, starting Monday at 10:00 Tehran
   time. If it starts later in the week, it can publish the previous full week
   once, provided qualifying ads exist.
5. Test the private \`/cargoreport\` command before enabling automatic posts.

The feature is **off by default**. No channel posts are attempted until opted
in. It reuses \`TELCLAW_TELEGRAM_BOT_TOKEN\` and the existing monitoring poller.

## Aggregation contract

The reporting period is the previous **complete** Monday 00:00 to Monday
00:00 in \`Asia/Tehran\`, with the end exclusive. Counts come from
\`messages\` + \`transferlist\` in local SQLite and include only records with:

- \`processing_status='processed'\`, \`ai_status='processed'\` and
  \`ai_category='transferlist'\`;
- normalized ISO-2 origin/destination country codes and nonempty cities;
- message timestamps within the report period (not departure dates).

Each sender, route, role and departure date is counted once, regardless of
reposts. Sender ID is preferred; otherwise the username, then the
\`(channel_username, message_id)\` pair is used. A sender may have distinct trips.
Only \`transfer_role='shipper'\` is counted as **requests** and
\`transfer_role='passenger'\` as **passenger offers**. Blank/unrecognized roles
remain **unknown**; they are never guessed. The extraction prompt may not fill
this optional legacy field, so the report always shows the unknown-role total.
These are listing counts, not completed shipments or available capacity.

The report ranks routes by unique listing count. The private widget contains a
snapshot with tabs for all listings, shipping requests and passenger offers.
The HTML stays within the Telegram widget limits (32 KB HTML, 4 KB zlib level
9); its \`public_data\` is capped at 1024 UTF-8 bytes and the top-route list
shrinks if necessary. Every user receives a fresh snapshot when requesting it.

## Publication safety

The \`cargo_weekly_report_publications\` table is created independently from
the existing pipeline schemas. A report period is claimed atomically **before**
the network call. On confirmed success, \`status=sent\` and Telegram message
ID are stored. If the Telegram response is lost, \`status=uncertain\` is stored
and the worker **does not automatically resend**: the post may already exist.
Check the channel and database status before manually recovering a period.

The report does not modify ads, classifications, transfer locations, delivery
queues, crawlers or AI state.

## Test coverage

\`tests/test_cargo_weekly_widget.py\` covers timezone week boundaries, role
accounting, deduplication, invalid locations, Telegram widget size and data
limits, the private bot deep link, repeated publishing and ambiguous network
outcomes, and the public/private bot command boundary.
