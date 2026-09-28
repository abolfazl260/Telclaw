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
   TELCLAW_BACKOFFICE_HOST=127.0.0.1
   TELCLAW_BACKOFFICE_PORT=8787
   ```

3. Configure a reverse proxy with a valid HTTPS certificate to forward the public
   URL to `http://127.0.0.1:8787`. Do not expose port 8787 directly to the internet.
   The browser session cookie is marked `Secure`, so the public URL must use HTTPS.
4. Restart Telclaw and send `/backoffice` to the bot in a private chat from an
   authorized Telegram account. The link expires after five minutes and can be
   redeemed only once. It establishes a 12-hour browser session. Use **Log out**
   when finished.

## Rules

First create a destination using its `@channel_username` or a numeric Telegram
group/channel chat ID such as `-100...`. Then create a rule:

- **Category:** transport (`transferlist`), housing (`housinglist`), or jobs
  (`joblist`).
- **Country:** optional ISO two-letter code such as `TR`. For transport, choose
  whether it must match the origin, destination, or either. Housing checks its
  `country_code`; job listings have no structured country field and should use
  category-only rules.
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
not repeated for the same destination. Failed sends are retried on later cycles.
Telegram posting must be enabled by granting the bot posting rights at each target.
