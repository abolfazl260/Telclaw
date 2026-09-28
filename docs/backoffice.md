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

Add each destination through the channel dialog, including its Telegram username or
negative chat ID and an optional description. The bot checks membership and posting
permission when you save; use **Check connection** to refresh the result. Channels
require the bot to be an administrator with permission to post. The **Delivery history**
button shows the last 30 attempts for that destination, including Telegram message IDs,
errors, and a retry action. A Telegram 403 pauses further attempts for that destination
until its permission is fixed and the connection check succeeds, which requeues rejected
403 deliveries.

Each destination is one expandable row containing its settings, rules, and delivery
history. Add multiple rules inside the destination they publish to. For each rule,
select the advertisement category and then one of that category's fields in
**Category field**. The next dropdown loads distinct stored values for that field
from AI-processed ads in SQLite (up to 120 short values). Pick the value to match,
such as `origin_country` → `TR`. If no stored values exist yet, use a category-only
rule or wait until an ad with the relevant data has been processed. The previous
country, city, and price conditions remain in the expandable section for existing
rules; conditions in one rule all have to match.

Each rule displays the number of matching processed ads and how many were sent to
its destination, including sends made by another rule for that destination.
Expand **Matching ads** to browse 20 at a time and read the full ad preview. New rules default
to **Manual selection**: click **Send this ad** on an individual match to publish it.
Existing rules retain automatic delivery until switched to manual in **Edit rule**.
Already delivered ads show **Send again** in **Matching ads**. Resending requires an
explicit browser confirmation, keeps the original successful delivery unchanged, and
stores each resend attempt separately in delivery history as a `resend` entry.
**Delete rule** removes only the rule, leaving past delivery history intact.

If Telegram returns HTTP 429, publication pauses for the `retry_after` time returned
by Telegram, including after process restarts. The affected ad remains retryable and
automatic publishing resumes when the pause expires. Manual sends can be attempted
again once the pause expires.

If the service stops during a Telegram request, the delivery becomes **uncertain**
after five minutes. Check whether the ad appeared in the channel before using
**Review and retry**; Telegram does not provide an idempotency key for `sendMessage`.

First create a destination using its `@channel_username` or a numeric Telegram
group/channel chat ID such as `-100...`. Then create a rule:

- **Category:** transport (`transferlist`), housing (`housinglist`), or jobs
  (`joblist`).
- **Country:** optional ISO two-letter code such as `TR`. For transport, choose
  whether it must match the origin, destination, or either. Housing checks its
  `country_code`; job listings have no structured country field and should use
  category-only rules.
- **Other conditions:** source Telegram channel, exact origin and destination city, and minimum/maximum numeric price. All populated conditions in one rule must match.
- **Priority:** smaller numbers run first. The first matching rule wins by default.
  Check **Continue** to allow later matching rules to publish to additional
  destinations. Repeated matches to the same destination are deduplicated.
- **Enabled:** uncheck to pause a rule or destination without deleting its history.

With no matching rule, a message is not published. Activating the back office
replaces the previous fixed transfer-channel publisher for that process. Existing
AI-processed records without entries in the new delivery table are eligible for
the new rules, including historical records. Review those records before enabling
any broad rule if you do not intend to publish older messages.

Each message and destination has a separate delivery record. A successful send is
not repeated for the same destination. Failed sends are retried on later cycles. The page lists recent delivery results; rejected items can be retried manually after the underlying issue is fixed.
Telegram posting must be enabled by granting the bot posting rights at each target.
