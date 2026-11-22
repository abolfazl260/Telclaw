# Telegram Private Topic Sidebar (three admin topics)

Telclaw's Telegram Monitor automatically manages three private-chat topics for every authorized administrator. There is **no separate feature flag, .env toggle, or Backoffice setting** for Topic Sidebar.

| Topic | Messages |
|---|---|
| داشبورد و مدیریت | Reports, status, administration commands, document downloads, Rich Messages and Widgets |
| هشدارها | WARNING notifications and their original-source message context |
| خطاها | ERROR / CRITICAL notifications and their original-source message context |

Report content, administrator permissions, and business logic are unchanged. Channel publishing is not routed to private topics.

## Normal operation

1. Deploy the latest Telclaw code and restart the Telegram Monitor. When its usual bot token and monitoring service are enabled, the monitor automatically checks the bot settings with `getMe` and, if needed, calls `setMySettings` to turn on `has_topics_enabled` and `has_topic_sidebar` and turn off `allows_users_to_create_topics`. Other bot settings, including `callback_query_mode`, remain unchanged.
2. Each authorized administrator sends `/start` in their own private chat. The bot provisions any missing topics, saves thread IDs to the `telegram_monitor_private_topics` SQLite table, and reuses the saved IDs on subsequent `/start` commands.
3. Routine reports are routed to Dashboard; WARNING to Warnings; ERROR/CRITICAL to Errors. Replies to commands and callback interactions stay in the originating topic.

No manual BotFather operation is needed when Telegram accepts `setMySettings`. No separate activation step in Backoffice or server `.env` is available or required.

## Scope, fallback, and troubleshooting

- Telegram's topic/sidebar setting applies to the **whole bot**, including the visual experience for ordinary users. Telclaw only creates and routes the three administrator topics for authorized administrators. Public commands remain available.
- Telegram clients may also show a built-in General topic. Previous chat messages are not moved.
- Topic creation can succeed even if the network response times out. Automatic retries for ambiguous failures are intentionally avoided; check the chat before manually repeating `/start`.
- An HTTP 400 response definitively identifying a deleted/invalid topic causes a one-time resend to the main private chat. The invalid mapping is dropped; the next `/start` can provision it again. Network failures and rate limits are not automatically resent.
- If Telegram rejects `setMySettings` or its API is unavailable, the monitor continues to operate with the existing legacy chat delivery path and logs a warning; no separate feature switch is needed.
- This functionality is part of the Telegram Monitor. If the entire monitor is not running or has no configured token, no topics can be managed.

## Tests

```bash
python -m pytest -q tests/test_private_topics.py tests/test_telegram_topic_routing.py
python -m pytest -q tests
```

After deployment, verify the three topics and alert routing in an administrator's Telegram client.
