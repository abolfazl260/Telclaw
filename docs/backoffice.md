# Telclaw publishing back office

The back office is disabled by default. It runs inside the existing Python process
and stores targets, rules, login sessions, and per-destination delivery records in
the existing SQLite database. Only Telegram admins `1485409432`, `266809220`, and
`7469291969` can request a login link in a private chat with the monitoring bot.

## Start

1. Give the bot permission to post in every destination channel or group.
2. Put these settings in the server's `.env` file:

   ```dotenv
   TELCLAW_TELEGRAM_MONITOR_ENABLED=true
   TELCLAW_TELEGRAM_BOT_TOKEN=your_bot_token
   TELCLAW_BACKOFFICE_ENABLED=true
   TELCLAW_BACKOFFICE_PUBLIC_URL=https://your-private-domain.example
   TELCLAW_BACKOFFICE_PUBLIC_PORT=0
   TELCLAW_BACKOFFICE_HOST=127.0.0.1
   TELCLAW_BACKOFFICE_PORT=8787
   ```

3. Configure a reverse proxy with a valid HTTPS certificate to forward the public
   URL to `http://127.0.0.1:8787`. Do not expose port 8787 directly to the internet.
   The browser session cookie is marked `Secure`, so the public URL must use HTTPS.
   For direct access by server IP and port without a reverse proxy, set:

   ```dotenv
   TELCLAW_BACKOFFICE_PUBLIC_URL=https://SERVER_PUBLIC_IP:8787
   TELCLAW_BACKOFFICE_PUBLIC_PORT=0
   TELCLAW_BACKOFFICE_HOST=0.0.0.0
   TELCLAW_BACKOFFICE_PORT=8787
   TELCLAW_BACKOFFICE_TLS_CERT=/path/to/ip-certificate.pem
   TELCLAW_BACKOFFICE_TLS_KEY=/path/to/private-key.pem
   ```

   The certificate must be valid for that IP address. Allow incoming TCP 8787
   through the server firewall. Keep the TLS private key readable only by the
   service account. Restart Telclaw and request a fresh link from the bot.
   If you explicitly want direct HTTP without TLS, use:

   ```dotenv
   TELCLAW_BACKOFFICE_PUBLIC_URL=http://SERVER_PUBLIC_IP:8787
   TELCLAW_BACKOFFICE_PUBLIC_PORT=0
   TELCLAW_BACKOFFICE_HOST=0.0.0.0
   TELCLAW_BACKOFFICE_PORT=8787
   TELCLAW_BACKOFFICE_TLS_CERT=
   TELCLAW_BACKOFFICE_TLS_KEY=
   ```

   Open TCP port 8787 in the firewall. Direct HTTP exposes the one-time login
   link and browser session to anyone who can observe the network connection.
   To include a port in the Telegram link, set `TELCLAW_BACKOFFICE_PUBLIC_PORT=8443`
   and configure the HTTPS reverse proxy to listen on 8443. A public IP can replace
   the domain in `TELCLAW_BACKOFFICE_PUBLIC_URL` only if HTTPS has a certificate valid
   for that IP. The public port and local 8787 port serve different purposes.
4. Restart Telclaw and send `/backoffice` to the bot in a private chat from an
   authorized Telegram account. The link expires after five minutes and can be
   redeemed only once. It establishes a 12-hour browser session. Use **Log out**
   when finished.

## System Health tab

The **System Health** tab is a read-only operational dashboard. It shows SQLite
integrity and size, tracked table row counts, current processing/classification/AI/
Advertio queues and failures, recent crawl totals by day, and every configured or
observed crawl channel with its first/last stored message and failure count.

Crawler, processing, classification, AI and Advertio reports emitted by the Telegram
monitor are now persisted in the `system_activity` table before they are broadcast.
System errors are persisted there as well, so the health tab keeps a recent activity
timeline across browser refreshes and process restarts. The page also reports Telegram
monitor runtime state, active report subscribers, publishing delivery/resend totals,
and recent audited database edits. Configured channels that have never produced a
stored message are shown as **not crawled** instead of silently disappearing.

The dashboard only reads operational state; it does not start, stop or retry pipeline
jobs. Use the page's **Refresh** link to request a fresh snapshot.

## Database tab

The **Database** tab shows `messages`, `transferlist`, `housinglist`, and `joblist`
as paginated tables (25 rows per page, newest IDs first). Scroll horizontally to
inspect every column. Click a blue cell to edit its complete value or set it to
`NULL`, then save. Updates are checked against the value loaded when the cell was
opened; if the crawler changed it meanwhile, reload the cell before saving again.
Every successful edit is recorded in `backoffice_data_edits` with the admin ID.
IDs, foreign keys, pipeline status fields, and authentication tables cannot be
edited here. Changes to ad fields are immediately available to publishing rules;
already sent ads are not automatically resent.

## Rules

Add each **publishing channel / group** with its Telegram username or negative chat ID
and an optional description. A channel/group is only the place where matched ads are
published; it is not a transport "destination" and does not imply any business topic.
The bot checks membership and posting permission when you save it. Use **Check connection**
to refresh that state.

Rules are topic-agnostic. Pick a structured category, then build zero or more conditions
from the columns that actually exist in that category's SQLite table. The back office
discovers those fields with SQLite schema metadata instead of maintaining a transport
field list in the UI. This means housing, jobs, transport, and future structured topics
all use the same rule engine. A newly added column is automatically available to rules
without adding a custom country/city/origin/destination control.

Each condition contains a database field, an operator, and (when needed) a value. Multiple
conditions can be connected with **AND** or **OR** and are evaluated from left to right.
Supported operators are equality/inequality, contains/not-contains, numeric comparisons,
and empty/not-empty. Stored values from the selected field are loaded as suggestions,
but admins can type another value when appropriate. The optional source-channel filter,
priority, enabled state, continuation behavior, and manual/automatic publishing mode are
shared controls that apply to every topic.

Older rules created with the previous country/origin/destination/price columns remain
compatible. When such a rule is opened, those legacy settings are translated into the
generic condition builder so saving the rule moves it to the topic-agnostic model without
changing its intended matching logic.

Each rule displays the number of matching stored ads and how many were sent to its
channel/group. Expand **Matching ads** to browse 20 at a time and read the full preview.
New rules default to **Manual selection**: click **Send this ad** on an individual match
to publish it. Existing rules retain automatic delivery until switched to manual.
Already delivered ads show **Send again**; resending requires explicit confirmation and
keeps the original successful delivery record unchanged.

**Delete rule** removes only the rule, leaving past delivery history intact. Rules run in
priority order. The first matching rule stops evaluation by default; enable **Continue
after match** to allow later matching rules to publish to additional channels/groups.
Repeated matches to the same channel/group are deduplicated.

If Telegram returns HTTP 429, publication pauses for the `retry_after` time returned by
Telegram, including after process restarts. If a send is interrupted, it becomes
**uncertain** after five minutes and requires manual review before retrying.

With no matching rule, a message is not published. Each message and publishing
channel/group has its own delivery record, and successful automatic sends are not repeated
for that same channel/group.
