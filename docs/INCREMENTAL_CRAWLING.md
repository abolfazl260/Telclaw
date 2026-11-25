# Scheduled incremental Telegram collection

Scheduled collection uses a durable SQLite checkpoint scoped to normalized
channel username, resolved Telegram peer ID, crawl mode and requested start
date. A new scope performs the requested full baseline. Subsequent scans use
Telethon min_id, also on reconnect. The cursor tracks the newest inspected
in-range ID (not the newest accepted ad). It is never inferred from old ads.
Historical end-date requests and direct/manual crawl calls remain full scans.

A successful crawl advances its checkpoint only after every eligible write
completes. Operator stop, cancellation, network failure and storage exceptions
leave the old checkpoint unchanged. A missed interval is recovered using the
last successful scan date with a day of overlap. Each scope performs a full
recent-window reconciliation every 24 hours (two calendar days by default,
extended after downtime). This is not a guarantee of detection of arbitrary
old edits. Edited text fingerprints are recorded in crawl_source_edits when
the original stored row is present; raw text and publication state are never
overwritten. This does not automatically republish edited ads.

Existing explicit Advertio source deletion checks are preserved, including on
reconciliation when a live anchor can be verified. No deletion is inferred
from gaps in message IDs. No new polling loop is added.

Active Channel Mirror sources deliberately retain the existing full scan
behavior, including album handling and rule activation semantics. Failed
mirror setup also falls back to full scan. They do not advance the incremental
cursor. Existing mirror queues, Telegram delay/rate controls, duplicate
checks and AI processing are unchanged. No live Telegram test is implied.

Back Office Health > Crawled channels shows the latest successful cursor,
new ID count (not necessarily accepted ads), and scan mode. The first crawl
after deployment establishes a baseline, not a guessed checkpoint.
