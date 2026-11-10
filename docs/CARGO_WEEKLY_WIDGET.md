# Advertio Cargo weekly route report (Telegram Bot API 10.4)

## Delivery surfaces

Telegram **production** currently supports HTML widgets in **private bot chats only**:
https://core.telegram.org/bots/widgets

- The channel receives a **button-free** Bot API HTML text summary, with the
  reporting dates, total listings, distinct route count, top five route totals,
  and the three plain mentions `@advertio_bot`, `@advertio_support`, and
  `@advertio_cargo`.
- No inline keyboard, widget deep link, breakdown by transfer role, or
  explanatory footnote is added to the channel post.
- Users may independently open the bot; \`/start cargo_weekly\` or the public
  \`/cargoreport\` command sends a real \`sendWidget\` message.
- No Mini App, web server, external scripts or widget network calls are needed.
- Unsupported/older clients can still read the widget's caption.
- Existing administrator-only monitoring commands and forward-import behavior
  are not exposed to regular users.

## Availability (enabled by default)

The weekly cargo report is a built-in Advertio feature. There are **no
cargo-report-specific environment variables or setup switches**.

- Deploy the updated Telclaw code with the existing Telegram monitoring bot.
- The public `/cargoreport` command is immediately available to users in
  their private chat with the bot; the bot token is already required for
  Telegram integration and is not a new reporting setting.
- The built-in channel destination is `@advertio_cargo`. The bot must have
  Telegram administrator permission to post there; no code-level or `.env`
  change is required.
- Every Monday at 10:00 Asia/Tehran or later, the worker publishes one report
  for the last completed week (only when qualifying ads exist). A later startup
  during the same week publishes at most once.
- With Telegram monitoring disabled or missing a bot token, the report
  cannot send Telegram messages. Publishing errors are logged; normal
  crawling and AI processing continue.

This uses the existing bot integration and does not modify other publishing
configurations.
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
remain **unknown**; they are never guessed. This PR adds explicit `transfer_role` extraction for new transferlist posts.
Historical records without that field remain unknown; no unsafe bulk reprocessing
is performed. The report always displays the unknown-role total.
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
limits, the button-free channel post, repeated publishing and ambiguous network
outcomes, and the public/private bot command boundary.
