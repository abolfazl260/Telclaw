# Channel Mirror media transport (#185)

Requires the queue/configuration and crawl staging from #183–184. Rules are still
disabled by default. Use the existing authorized crawler account, existing bot
token, and source enrollment described in [CHANNEL_MIRROR.md](CHANNEL_MIRROR.md).
There are no new credentials, real-time listeners, AI steps, media transformations,
automatic historical scans, or Back Office controls in this phase.

## Runtime and dispatch

After each successful, unstopped source crawl, `CrawlJobService` commits its queue
release and durable dispatch event. If a bot token is configured and no custom
trigger was injected, it lazily creates a `Dispatcher` and starts a background
drain for that source. Scanning never sends. A custom nonblocking trigger still
overrides the default integration. With no bot token, events/queue remain durable.

The drain claims only the completed source peer's due waiting/retry deliveries.
It processes at most 50 claims per activation, one at a time, with a 240-second
deadline per delivery and a 300-second fenced claim lease. Triggers for a source
coalesce, including triggers arriving during a drain. There is no permanent
polling loop or sleep until future retry times. Due backlog and definite retries
are eligible on subsequent successful source crawls; no sending resumes merely
because the application restarted. A dispatch event acknowledges an opportunity
to drain, not delivery success or ownership.

The main runtime and scheduler shutdown cancel these tasks. Cancellation marks an
active claim uncertain; expired claims are quarantined at startup and at the next
drain. A new process never blindly repeats a send whose result is unknown.

## Publication paths

Every attempt checks the bot's identity, destination channel ID, live public
username and `can_post_messages` permission. Queued captions retain their original
snapshot. A renamed/private/reassigned destination is an explicit failure until
an administrator resolves its configuration and queued delivery. The active rule
and unexpired claim are checked again immediately before Bot API mutations.

Before a new media send, the authorized Telethon client refetches the source
channel and ordered source messages. Protection, missing/changed media, changed
grouping and unsupported content are explicit failures. Source text, entities,
attribution and buttons are never read into an outgoing payload.

| Path | Bot API calls and caption behavior |
| --- | --- |
| Accessible standalone source | `copyMessage` with an explicit nonempty destination caption, empty caption entities and empty inline keyboard |
| Accessible photo/video album | `copyMessages(remove_caption=true)` in source order, then persist destination IDs and media timestamp, then `editMessageCaption` on the first destination ID |
| Source unavailable to the bot | Download through the existing authorized Telethon client and upload using `sendPhoto`, `sendVideo`, `sendDocument` or `sendMediaGroup` |

An album containing source inline keyboards uses the sanitized upload path because
`copyMessages` has no keyboard override. A standalone copy explicitly overrides
the keyboard. Upload albums specify the fixed caption on their first member and
empty captions on all other members. The selected parse mode is plain text:
HTML/Markdown characters in an admin footer are displayed literally, with no
source caption entities, buttons, links, or forwarded header inherited. Existing
text physically embedded in media pixels is not edited.

The fast path may fall back only after a definite source-access rejection; generic
non-copyable/protection errors never trigger an attempted bypass. Even when the
bot has access, source protection is checked through the crawler account first.

## Album checkpoint and retry safety

Additive startup migration adds `publish_stage`, `media_sent_at` and
`caption_set_at` to `cm_deliveries`. Existing queue states and destination IDs stay
intact. No new foreign keys point at legacy publisher queues.

| Persisted state | Permitted next operation |
| --- | --- |
| `new` | Send/copy one complete source post after preflight |
| `caption_pending` | Edit only the first known destination message's caption; never copy/upload again |
| `partial` | Quarantine for manual review; never try to rebuild an album automatically |
| `complete` | Terminal sent delivery, excluded from future claims |

For copied albums, all returned IDs and attempt evidence are committed **before**
the caption request. A structured edit rejection leaves the caption stage and IDs
intact across retry/restart. An idempotent “message is not modified” edit response
counts as success. Partial/malformed acknowledgements preserve every known ID and
become uncertain; they do not fall back to another copy. An expired/stale worker
cannot overwrite a new claim's state. Attempt history and known destination IDs
remain available to manual review.

Structured API 429/5xx rejections are definite retries, honoring `retry_after` and
the queue's capped exponential backoff/attempt limit. Ambiguous publication
responses, timeouts, connection drops and cancelled/expired sending claims become
uncertain. Read-only permission/source failures may retry without claiming a send
succeeded. Administrators must inspect Telegram before resolving uncertain work.
Manual retry of a `caption_pending` delivery still edits only its existing first
destination ID. A partial album must be resolved manually; automated retry never
creates replacement media posts.

## Fallback bytes, resource limits and session ownership

Fallback downloads only supported original media bytes, without re-encoding or
transcoding. Files use generated names in a private temporary directory (0700)
and individual files are 0600. Source filenames cannot select filesystem paths.
The download checks protection again before reading bytes, streams chunks to disk,
checks declared/actual sizes, and bounds each upload to 10 MiB for photos or
50 MiB for other media, with at most 500 MiB per album. Unsupported photo dimensions
are rejected instead of resized. Zero/truncated/oversized data is an explicit
failure. Bot API format constraints can also reject unsupported original media.

Multipart uploads use file handles, not whole-file buffers, with a bounded HTTP
timeout. Handles and temporary files are cleaned after success, rejection, error
and cooperative cancellation. `sendDocument` disables content-type detection to
preserve ordinary document treatment. No Telethon media IDs are misused as Bot API
file IDs; no image/video editing or Advertio downloader is involved.

`session_guard(client)` is shared by crawler scans/album refetch, mirror source
reads/downloads and account connect/disconnect. It borrows the live client without
opening/logging in another session, reconnecting or disconnecting it. The guard is
released before potentially slow Bot API uploads. Publication failures are isolated
in background tasks so they do not roll back the completed crawl or fail future
channels, classification, AI extraction or Advertio delivery.

## Shared bot limits

Startup also creates the publisher-independent `telegram_bot_limits` registry.
Only a SHA-256 token fingerprint is stored. Transactional reservations space
bot-wide mutations by 50ms per media unit and per-chat requests by 1.1 seconds.
The sleep happens outside SQLite. Shared 429 deadlines never shorten when a later
response supplies a shorter delay.

Mirror, Message Manager, routed/legacy transfer publishing and monitor sends report
429s to this registry and honor its pacing/cooldown. Their payloads, delivery
results and independent queue ownership remain separate. Isolated legacy clients
without application bootstrap retain their prior behavior when the registry is
absent. No new DDL runs per send; the registry has no `mm_*` or `publishing_*`
foreign keys.

## Verification and references

`tests/test_channel_mirror_transport.py` uses deterministic fake Bot API sessions
and fake authorized Telethon clients. It covers explicit caption/button removal,
mixed albums, checkpoint-before-edit, restart/cancellation, uncertain outcomes,
manual caption-only retry, source/destination protection and permissions, bounded
original-byte downloads, temporary-file cleanup, shared session ownership, 429
coordination and finite per-source dispatch. It creates no live Telegram posts.
A live smoke test requires an explicitly chosen test source/destination and a bot
with posting permission; no production channel was used for these tests.

Official Telegram contracts:
[copyMessage](https://core.telegram.org/bots/api#copymessage),
[copyMessages](https://core.telegram.org/bots/api#copymessages),
[editMessageCaption](https://core.telegram.org/bots/api#editmessagecaption),
[Sending files](https://core.telegram.org/bots/api#sending-files), and
[sendMediaGroup](https://core.telegram.org/bots/api#sendmediagroup).
