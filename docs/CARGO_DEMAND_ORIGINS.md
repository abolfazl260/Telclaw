# Advertio Cargo — Top 7 demand origins

The public `/cargoorigins` command sends a formatted Telegram **HTML text
message**, not a widget. The report works even when Telegram rejects
`sendWidget` with `INPUT_MEDIA_WIDGET_NOT_ALLOWED`.

## Usage

- `/cargoorigins` — top 7 origin **cities** for the past 30 rolling days
- `/cargoorigins 7` — 7 days
- `/cargoorigins 15` — 15 days
- `/cargoorigins 30` — 30 days

Persian numeral argument `۷`, `۱۵` or `۳۰` is also accepted. No inline
buttons, web assets, external API calls, new scheduled tasks, or configuration
flags are needed. The output includes seven ranked origin city/country entries,
number of unique requests, relative bar lengths and percentage of all eligible
requests, total requests and number of distinct origins, and the Tehran
timestamp.

## Metric definition

**Demand** means `transferlist.transfer_role='shipper'` (a request to send
cargo), not `passenger` (a carrier's offer). The report is based on processed
and AI-processed listings posted within the selected elapsed-day window. It
does **not** measure bookings, shipments completed, users served, or searches.

For each origin, the city and ISO alpha-2 country are treated together. When
present, cached canonical city values in `transfer_locations` are preferred;
otherwise the stored extracted city is used. Missing/invalid origin city or
country is excluded. The report does not mutate the database or re-run
normalization. One sender's reposts are deduplicated for identical country,
city, destination, role, and departure date when a complete route is present.
Incomplete routes retain message-level identity to avoid accidental merging.
The request count is based on all eligible origins, including those outside
the displayed top seven.

## Access and delivery

- Available in the requesting user's own private chat with the existing
  monitoring bot, without administrator rights or subscription.
- Group, channel, forwarded, and mismatched sender/chat requests are ignored.
- The bot directly calls `sendMessage` with Telegram HTML and no keyboard.
- An empty report returns a clear no-data notice plus period commands.
- The existing `/cargocountries` and `/cargoreport` are unchanged.

## Tests

`tests/test_cargo_demand_origins.py` verifies period boundaries, role and
status filtering, country/city grouping, duplicate handling, cache usage,
top-seven ordering, HTML escaping, empty reports, private-chat restrictions,
and absence of `sendWidget`.
