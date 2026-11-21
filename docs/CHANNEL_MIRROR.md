# Channel Mirror configuration, queue and crawl capture (#183–184)

The foundation adds configuration, media metadata, a durable queue, and review
contracts. The existing manual/scheduled crawler now captures media metadata and
releases it only after each successful source crawl. This **does not send any
messages**. Transport, a sending worker and Back Office UI belong to following
issues. There is no live listener, extra timer, historical catch-up, or source
edit/delete synchronization.

## Existing architecture and isolation

- `storage/database.py` stores advertisement `messages` using channel username and
  message ID; processing/classification/AI states do not describe mirror delivery.
- `routing_rules.py` owns category-based `publishing_*` tables and destinations.
- `message_manager/repository.py` owns `mm_*` scheduling, leases, and uncertain
  delivery review. Its transactional claim pattern is useful, but its schedules,
  target eligibility, rate limits, and content model are separate concerns.
- `storage/message_repository.py` already guards legacy bootstrap per database.
  `main._run()` explicitly initializes the mirror schema immediately after that
  bootstrap. Mirror migration never calls a legacy initializer or reads legacy
  rows. Mirror operations never initialize schema, backfill, or scan ad queues.

`channel_mirror.Repository` uses the configured SQLite connection factory, with
foreign keys, a busy timeout, and `BEGIN IMMEDIATE` for every mutation. Its
initializer runs additive `CREATE TABLE/INDEX IF NOT EXISTS` statements in one
transaction. It is safe to repeat on startup: it never changes existing data or
recovers leases as a migration side effect. All mirror foreign keys stay within
the `cm_*` namespace; it has no dependency on existing publishing targets.

## Durable records

| Table | Purpose |
| --- | --- |
| `cm_rules` | Stable source peer and destination chat IDs, resolvable source name, required public destination username, V1 mode, footer, state, activation boundary, creator/updater and timestamps |
| `cm_posts` | One source message or complete album, stable peer-scoped identity, first crawl run, observation timestamp |
| `cm_source_items` | Telegram message ID/date, grouped ID, photo/video/document kind, deterministic album position, first crawl run; no text or sender requirement |
| `cm_deliveries` | One post per destination, frozen caption/chat/username, status, attempts/limit, lease, retry time, destination ID vector, error, lifecycle timestamps |
| `cm_delivery_attempts` | Numbered attempts, unique claim tokens, outcomes, partial/complete destination ID vectors, error, start/finish times |
| `cm_audit` | Actor, action, entity ID, JSON details and UTC time for rule changes, outcomes, lease recovery and manual decisions |

Rules are unique per `(source_peer_id, destination_chat_id)`. Source peers can have
multiple destinations and destinations can receive from multiple sources. IDs are
signed 64-bit Telegram IDs, never usernames; callers must resolve/verify peer IDs
and the destination's public username before saving configuration. Source names
can be public usernames or another name resolvable by the future Telegram adapter.
Renaming a source or destination does not change identity. To change either stable
ID, disable the old rule and create a new rule. No physical deletion API is exposed.

## Rule and watermark semantics

All dates are timezone-aware and stored as fixed-width UTC ISO timestamps.

| Action | Ingestion and existing deliveries |
| --- | --- |
| Create | Disabled, `media_only`, no activation boundary; never automatically activates |
| Activate | Save the activation time as the exclusive lower boundary; reject messages at or before it |
| Edit source name, username or footer | Only subsequently created deliveries use the new metadata; existing captions and destinations stay frozen |
| Pause | Stop new deliveries and claims; retain waiting/retry deliveries; already claimed work can finish |
| Resume | Start a new boundary at resume time, excluding the paused gap; previously queued waiting/retry deliveries become claimable again |
| Disable | Stop new deliveries and claims; mark waiting/retry deliveries skipped; retain in-flight, sent, failed and uncertain records |
| Reactivate | Start a new boundary; skipped deliveries remain skipped and previously delivered posts remain deduplicated |
| Activate an already active rule | No-op; does not advance the boundary |

Boundaries never move before creation or a previous activation even if the clock
moves backwards. Caption edits do not move the boundary. A new destination gets its
own activation boundary. Repeated scans cannot override the first persisted source
dates to manufacture eligibility. The oldest album member must be strictly after
the boundary, so an album cannot be partially backfilled across activation.

V1's caption is plain text: `@destination` followed, when nonempty, by two newlines
and the exact configured footer. The snapshot is limited to 1024 UTF-16 units.
Source captions, text, inline buttons, and AI output are never inputs. A later
transport must use plain-text captions without interpreting HTML/Markdown.

## Low-level media repository contract

This direct API is for complete posts, outside crawl orchestration. Production
crawls use the staging/completion boundary described below and never call it
while scanning.

```python
from datetime import datetime, timezone
from channel_mirror import Repository, Service, SourceItem

repository = Repository()  # initialize() is performed only by startup
service = Service(repository)
new_ids = service.observe_media(
    source_peer_id=-1001234567890,
    items=(SourceItem(123, datetime.now(timezone.utc), "photo"),),
    crawl_run_id="durable-crawl-run-id",
)
```

Only photos, videos and documents are accepted as standalone posts. Text-only,
text-and-media, unsupported media types and missing dates/crawl IDs are rejected.
There is no dependence on `messages`, cleaned text, usernames of senders, content
hashes, AI, downloads, or legacy media-size thresholds.

For an album, pass **all** members from one peer and grouped ID in a single call
with `album_complete=True`. The caller must assemble and confirm completeness;
this flag cannot discover missing Telegram members by itself. Partial albums must
not enter this API. V1 accepts 2–10 photo/video members, sorts by Telegram message
ID, and rejects mixed groups, duplicate members and document albums. Member
message IDs are unique per source peer across all posts. Once recorded, an album's
membership is immutable; a conflicting later scan fails atomically for review.

Delivery uniqueness is `(post_id, destination_chat_id)`, where the post identity is
`message:<message_id>` or `album:<grouped_id>` scoped to the stable source peer.
Duplicate scans/runs/restarts return no newly created IDs. Metadata can be stored
without an eligible rule; later scans still use the original dates. No historical
scan is scheduled by this package. Unsupported or incomplete input is rejected
before persistence, rather than inventing individual album deliveries.

## Queue and failure recovery contract

`claim(now=..., lease_seconds=300)` atomically selects one active rule's due
waiting/retry delivery, increments attempts, records a unique lease token and
opens its numbered attempt. It returns the frozen caption, chat ID and ordered
source items. Separate processes cannot claim the same delivery. Nothing in this
phase calls `claim` automatically or performs a network send.

`finish(delivery_id, token, SendOutcome(...))` only accepts a matching, unexpired
claim. Repeated completion or stale worker results return `False`. Outcomes:

| Outcome | Behavior |
| --- | --- |
| `sent` | Require one destination ID for every source member, preserving order |
| `retry` | Means definitely unsent; persist exponential backoff (30s, 60s, ... capped at 1h), honoring a larger `retry_after`; three automatic attempts by default, then failed |
| `failed` | Terminal definite failure, retained for review |
| `uncertain` | Ambiguous send; quarantine until an explicit admin decision |
| `skipped` | Terminal intentional omission |

A partial album ID vector, missing sent IDs, or IDs attached to any nonsent
outcome automatically produces `uncertain`, retaining every known ID. A disabled
rule converts an in-flight definitely-unsent retry result to skipped. Pausing or
disabling cannot retract a send already in flight, so its outcome remains recorded.

A future worker must periodically call `recover_expired()`, including on restart.
It marks expired sending deliveries and open attempts uncertain, never waiting or
retry. Initialization intentionally preserves queue state. The future transport
must classify ambiguous network failures as uncertain, respect the claim lease,
and stop expired workers before manually authorizing a resend. SQLite cannot
guarantee exactly-once external delivery after a process/network failure.

`review(delivery_id, action, actor, reason, ...)` accepts only uncertain/failed
deliveries and requires a nonempty reason. The administrator checks Telegram and:

- confirms `sent` with the complete ordered destination ID vector;
- chooses `skipped` to abandon the delivery;
- requests `retry` on an active rule with `confirm_duplicate_risk=True`.

Manual retry preserves the monotonic attempt count and grants at least one further
attempt after the automatic limit. Partial IDs and uncertain results remain in
attempt history; a review never rewrites historical attempts. Paused/disabled rules
cannot be manually retried. Read APIs expose rules, deliveries, attempts, and audit
history for the following admin UI issue. Queue state and all review actions survive
restarts.

## Validation

`tests/test_channel_mirror.py` exercises additive/idempotent/transactional migration,
legacy state isolation, disabled defaults, V1 validation, snapshots, relationship
fan-out, peer identity, restart deduplication, complete album identity/order,
activation/pause/reactivation boundaries, threaded and independent-process claims,
backoff/limits, stale leases/tokens, partial outcomes and manual review.

## Existing crawler integration (#184)

`CrawlJobService.run_channel()` is the shared manual/recurring boundary. With at
least one active mirror rule it starts a durable run and passes an optional
`mirror_capture` object through `CollectionService` to `crawl_channel`. With no
active rules there is no capture or additional Telegram resolution. Schema setup
stays in application startup, never in channel crawls. Direct callers of the
low-level crawler must explicitly provide a capture; they do not auto-release it.

After resolving the source, capture binds to its marked Telegram peer ID and
snapshots the IDs/activation boundaries of its active rules. A username rename
does not change that identity; reassignment to another peer cannot redirect a
rule. Rules activated/resumed mid-run participate on a later run. Footer edits
are evaluated when the delivery snapshot is created at successful completion.

The scan callback executes after the existing inclusive date-range check but
**before** forwarded-origin, bot, sender-username, ten-word, photos-only and content
dedup filters. It reads media IDs/dates/grouped IDs, kind, and whitelisted file
metadata only. The existing ad filters, saved raw text/captions, generic document
typing, photo downloader, processing/AI/Advertio behavior remain intact. Empty and
service messages are ignored. Source/channel `noforwards`, protected individual
posts and expiring media are excluded. Stickers, voice/audio, GIF animations and
round video notes are not ordinary V1 media documents. Video attributes or a
`video/*` MIME type distinguish videos from regular documents.

Capture writes only `cm_crawl_items`, keyed by run/message ID. It never inserts a
delivery during the scan. Only media newer than the oldest captured rule boundary
needs staging. There are no media downloads and no AI calls in this path.

Albums are assembled after the source scan, once per grouped ID. A bounded ID
refetch includes ten neighbors on each side of the observed group, with at most
40 requested IDs per group. Telethon's positional list response, including `None`
for deleted IDs, is checked before accepting the group. A group reaching a window
edge without all ten possible members, an excessive span, malformed response, or
network error fails capture without releasing any of that run. Protected or
unsupported groups and surviving singletons are omitted as a whole. Complete
photo/video members are sorted by message ID. Members outside a scan page or date
boundary can be included to preserve the entire album; the oldest member must
still pass each destination's activation watermark. No album is split into posts.

Mirror RPCs have a 15-second deadline and reuse the existing bounded reconnect
policy (three retries). Default crawl repositories use a 250ms SQLite busy timeout
so mirror lock contention cannot stall the ad collector for seconds. Any bind,
capture, album, or release failure is isolated and reported separately in the
crawl result's `mirror` entry. Ad collection is never rerun to retry mirroring.

| Additional table | Purpose |
| --- | --- |
| `cm_crawl_runs` | Persistent source/range/mode, stable peer, rule generation snapshot, run state, timestamps, renewable lease and error |
| `cm_crawl_items` | Unreleased media metadata and verified complete album members; retained even for failed/interrupted runs |
| `cm_dispatch_events` | Durable per-completed-channel trigger committed with that run's deliveries; explicit acknowledgement for the following transport phase |

After `status=completed`, the job verifies it was not stopped/cancelled, finishes
album assembly, and atomically inserts all eligible posts/deliveries, marks the
run completed, and inserts its dispatch event. Any error rolls the entire release
back. Paused/disabled or reactivated rules are rechecked in that transaction.
Successful configured sources emit a trigger even when deduplication creates no
new deliveries. Unrelated/unconfigured/protected sources emit none.

Failed, operator-stopped and task-cancelled runs become `failed`, `stopped` and
`interrupted`; mirror-only failures become `mirror_failed`. None release staged
items. Run leases last ten minutes and refresh during capture/refetch. Startup
`recover_crawls()` marks **only expired** running records interrupted, preserving
live runs and all staging. This is separate from delivery-lease recovery. A
restart or next successful overlapping scan re-observes source media and safely
deduplicates it; unreleased records of failed runs are never blindly published.
Recovery across dates requires an existing crawl over the affected date range;
there is no automatic historical catch-up or independent scan.

`pending_dispatches()` exposes the durable outbox. An optional injected
`dispatch_trigger(event)` provides a synchronous, nonblocking wake-up (for example
`Event.set()` through a lambda or `Queue.put_nowait`). Its failure leaves the
committed event pending. It must not send inline, run AI, or return a coroutine.
The future transport acknowledges events with `acknowledge_dispatch(event_id)`.
This issue does not install a sending worker. Dispatch happens per completed
channel, before the scheduler waits for remaining channels or starts its existing
full-cycle processing/AI pipeline.

### Enrolling sources and reporting status

Creating/activating a mirror rule does **not** schedule a source. Add its public
username to the appropriate existing `channels.json` category using the inventory's
normal entry shape, such as `{"username": "source_channel", "enabled": true}`.
Select that category/source in the existing crawl controls and start a manual run
or recurring schedule. Entries with `enabled: false` and invite-only entries
without a username are not enrolled by the category scheduler. A rule's date
boundary remains authoritative even during a first crawl over older dates.

The scheduler registers its selected source names with the shared `CrawlJobService`
and removes them when tasks finish or stop. `mirror_status()` reports `scheduled`
for a selected source, `crawling` only for a currently bound, healthy capture, and
`waiting_not_scheduled` for an active rule without an active selected source.
Paused/disabled rules keep those states. Persisted running records alone never
claim a source is actively mirroring after restart. Known selector-to-peer
resolutions take precedence over mutable names; unseen names remain pending
resolution. The following admin UI can consume this contract without adding a
timer or another channel inventory.

`tests/test_channel_mirror_crawl.py` covers both crawl modes, unchanged ad filtering,
forwarded/textless/no-username/bot media, complete albums across scan boundaries,
protected content, stable identity, watermarks/reactivation, durable staging and
outbox, failures/cancel/stop/reconnect, startup recovery, scheduler registration,
and per-channel dispatch before the rest of the cycle's AI pipeline.
