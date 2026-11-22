# Telegram Private Topic Sidebar (three admin topics)

This optional feature organizes the existing Telclaw monitoring bot's private conversations into three topics for each authorized administrator.

| Topic | Messages |
|---|---|
| داشبورد و مدیریت | Reports, status, administration commands, document downloads, Rich Messages and Widgets |
| هشدارها | WARNING notifications and their original-source message context |
| خطاها | ERROR / CRITICAL notifications and their original-source message context |

All report text, command permissions, and business logic are unchanged. Channel publishing is not routed to private topics.

## Enable after code review

1. Deploy the code with `TELCLAW_TELEGRAM_PRIVATE_TOPICS_ENABLED=false` (the default). No Telegram bot settings are altered while this flag is disabled.
2. Set `TELCLAW_TELEGRAM_PRIVATE_TOPICS_ENABLED=true` and restart the monitoring bot. The bot reads its settings with `getMe`; if needed, it calls `setMySettings` with `has_topics_enabled=true`, `has_topic_sidebar=true`, and `allows_users_to_create_topics=false`. Other bot settings, including `callback_query_mode`, are not changed.
3. Every authorized administrator sends `/start` in their own private chat. Three topics are created on first use. The IDs are stored in SQLite (`telegram_monitor_private_topics`). Repeated `/start` reuses the saved IDs.
4. Check a routine report in Dashboard, a WARNING in Warnings, and an ERROR/CRITICAL in Errors. Responses to commands and callback buttons should appear in the originating topic.

## Important scope and limitations

- Telegram's sidebar setting applies to the **entire bot**, but Telclaw provisions administrative topics only for authorized administrator IDs. Public commands remain available.
- Telegram clients may show their own General topic in addition to the three named topics. The bot does not attempt to remove General or relocate old messages.
- A Telegram API request for topic creation that times out may have succeeded; automatic retries are intentionally avoided. Operators should inspect the chat before repeating `/start` after an ambiguous network error.
- For a confirmed HTTP 400 invalid/deleted topic, the send is retried once into the main private chat; the topic mapping is removed and a later `/start` recreates it. Network timeouts and rate-limit responses are **not** resent automatically (duplicates are possible otherwise).
- Routing applies only to sends through the Telclaw Telegram Monitor. Other independent Telegram publishers are not modified.

## Disable and rollback

Set `TELCLAW_TELEGRAM_PRIVATE_TOPICS_ENABLED=false` and restart. The bot returns to the legacy chat-wide delivery path, without deleting topics or SQLite mappings.

**Important:** Disabling this environment flag does not automatically revert Telegram's global bot settings. To disable Topic Sidebar across the bot, change the corresponding settings explicitly with `setMySettings` after considering the impact on users.

## Tests

```bash
python -m pytest -q tests/test_private_topics.py tests/test_telegram_monitor_access.py tests/test_admin_error_notifications.py
```

Check the full regression suite and the Telegram client behavior before merging and deploying.
