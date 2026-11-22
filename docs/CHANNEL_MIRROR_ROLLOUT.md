# Channel Mirror administration and staged rollout (#186)

The authenticated Back Office has a separate **Channel Mirror** tab at
`/channel-mirror`. All changes are POST actions protected by the existing session,
admin allowlist and CSRF middleware. Preview responses are plain text; HTML views
escape configured values. CSP/no-store headers apply. No route publishes media.

## Configure without SQL

1. Connect the authorized crawler account in **Operations** and configure the
   existing bot token in the normal runtime configuration.
2. In **Channel Mirror**, enter the source channel, destination channel reference,
   required public destination `@username`, and multiline **Fixed Footer**.
3. Preview the exact caption, including whitespace. Preview performs no writes or
   Telegram publication. The destination handle is always present even with an
   empty extra footer. The 1024 UTF-16-unit limit is enforced. Media Only is the
   only supported mode; future text modes cannot be enabled by submitting a form.
4. Save. Telegram source access/protection and destination identity/bot posting
   rights are checked live. New rules are disabled. IDs remain immutable; for an
   identity change disable the mapping and create a new one. Rename/footer edits
   keep existing delivery snapshots unchanged.
5. Use **Recheck live access and bot rights** to verify current permissions.
   The dashboard displays the timestamp of the last successful verification,
   not a promise that permissions have remained unchanged since that instant.
6. For a source absent from enabled inventory, explicitly enroll it into an
   existing category using the enrollment form. This safely updates the existing
   `channels.json` through an atomic replacement, with case-insensitive deduplication.
   It does not start a new crawler/timer. Go to Operations, select the category
   and start the existing manual or recurring crawler.
7. Explicitly confirm activation. It rechecks live permissions and sets an
   exclusive current-time watermark. Older messages are not backfilled. A source
   outside an active selected schedule remains `waiting_not_scheduled`, even if
   its rule is active. Pause/resume semantics are documented in CHANNEL_MIRROR.md.

The dashboard shows per-rule queue states, last completed source crawl, boundary,
last error, and live schedule status. Select a rule for paginated history (50 rows),
ordered destination IDs/Telegram links, and manual failed/uncertain review. The
last 50 audit entries include actor/action/timestamps and sanitized JSON details.
Errors/audit display redact the configured bot token and token-shaped strings.
Source bytes and captions are never included in these administration records.

## Manual uncertainty and rollback

Inspect Telegram first. Confirm `sent` with all ordered destination IDs, choose
`skipped`, or explicitly acknowledge duplicate risk before requesting a retry.
Caption-pending retries edit the known post only; a partial album cannot be
automatically reconstructed. HTTP requests only record state, never drain a queue.

The global switch is persisted in `cm_settings`, independently of other queues.
**Stop all mirror sends** blocks new capture, release and claims, and fences every
not-yet-issued Bot API mutation. It does not stop the crawler, Message Manager,
routed publishing or Advertio, and does not delete queued state or audit history.
An already-issued Telegram request cannot be recalled. In-flight outcomes still
need to be recorded or quarantined as uncertain.

Resume preserves waiting deliveries but advances active rules' boundaries to the
resume time, excluding the disabled gap. No sender runs merely because the switch
changed; delivery waits for a successful source crawl. For rollback, leave the
global switch off, disable individual rules if appropriate, retain the additive
tables, and revert application code only after stopping in-flight mirror work.

## Staging acceptance checklist — live steps pending

Automated tests use fake Telegram. The following require a specifically designated
test source, public test destination, authorized crawler session and bot with
`can_post_messages`. Do not use production channels for automated tests.

- [ ] Record test source/destination IDs, bot identity, operator and deployment SHA.
- [ ] Keep global mirroring off while configuring disabled test rules.
- [ ] Verify source access and destination rights; prove denial produces an explicit error.
- [ ] Select/enroll the test source through the existing crawler settings.
- [ ] Enable only the test mapping and confirm its watermark; old media must stay unsent.
- [ ] Crawl a new textless video, photo and ordinary document. Check media type and
      caption: only the destination handle and exact fixed footer; no source buttons/header.
- [ ] Crawl a mixed photo/video album carrying source captions; verify one ordered
      group, one footer, and no original captions. Check checkpoint IDs in history.
- [ ] Repeat the crawl and restart the app; confirm no duplicate sent/uncertain posts.
- [ ] Stop/fail a crawl before completion; confirm no partial release.
- [ ] Use a source readable only by the crawler account; verify upload fallback.
- [ ] Exercise a controlled 429/5xx rejection through a test API stub; do not flood
      Telegram deliberately. Verify retry delay and unchanged legacy queues.
- [ ] Simulate a dropped publication response and caption-edit failure with a test
      proxy/stub; verify uncertainty/manual acknowledgement and caption-only retry.
- [ ] Stop global mirroring during a staged test and confirm crawler/other pipelines
      continue, claims stop, and no unsent work is blindly replayed on resume.
- [ ] Record results and outstanding failures before production activation.

After this checklist succeeds, explicitly choose production mappings, revalidate
permissions/enrollment, and activate them individually. A green unit-test suite
does not certify that a real bot/channel deployment has passed the checklist.

## Automated regression

Install `requirements.txt`, `requirements-mcp.txt`, `pytest` and `pytest-asyncio`,
set placeholder `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`, and run
`python -m pytest -q tests`. GitHub CI uses Python 3.11. On the local Python 3.12
runtime, the previously documented cargo error-notification timing test can fail
independently of Channel Mirror. Tests cover authentication/CSRF/XSS, exact preview,
UTF-16 limits, live validation denial, explicit activation/review, independent
mappings, kill-switch fencing, enrollment, and existing fake-Telegram integration.
