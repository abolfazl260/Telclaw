# Advertio Cargo — origin-country widget

The `/cargocountries` command is a public **private-chat-only** Telegram Bot API
10.4 HTML widget. It is available by default through the existing Telclaw
monitoring bot; **no new `.env` settings or channel changes are required**.

## Interaction

- Three tabs at the top: **7 days**, **15 days**, **30 days**.
- A horizontal bar chart ranks countries by the number of eligible listings
  posted within the selected rolling period up to the current instant.
- Switches are instant and client-side; all three precomputed windows are
  included with the original widget message, with no follow-up bot requests.
- The widget shows the top eight origin countries, their counts and the
  aggregate `Other countries` count. It also shows the total eligible
  listings and number of distinct origin countries in the selected period.
- The date/time is shown in `Asia/Tehran`. Counts represent listing posts,
  **not completed shipments or unique passengers**.

## Counting contract

- Read only the local `messages` + `transferlist` tables, with AI and
  processing statuses both `processed` and category `transferlist`.
- Only valid two-letter origin-country codes are eligible; a valid
  destination or city is **not required** for a country breakdown.
- Deduplicate known reposts of the same trip by the same sender when full
  origin/destination location data is available. With incomplete locations,
  retain distinct message IDs rather than risk deduplicating unrelated ads.
- Read the previous 30 days in one SQLite query; count progressively over
  7, 15 and 30 elapsed 24-hour periods, including the lower endpoint and
  excluding future timestamps. Naive database timestamps are treated as UTC.
- Select the union of the eight leading countries for each period (up to 24
  country rows). Preserve full period totals so the chart can calculate
  `Other countries` correctly. All state stays below the Bot API 1024-byte
  `public_data` limit, and the HTML stays under 32KB raw / 4KB zlib.

## Security / delivery

The bot only accepts the command in the requesting user's own private chat.
Unauthenticated users cannot call administrator-only commands. The widget
is static and has `allow_queries=false`; no user identifiers, contact
information, or raw message text are sent in its state. Telegram currently
allows production `sendWidget` only in private chats; channel posts remain
unchanged, as does `/cargoreport`.

## Tests

`tests/test_cargo_countries_widget.py` checks time-window boundaries,
deduplication, invalid countries, incomplete routes, no-data display, Telegram
payload size, client-side tabs, and private-chat access.
