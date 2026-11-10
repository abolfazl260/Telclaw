# Public `/qa` — Persian Advertio Cargo FAQ

The Telclaw monitoring bot exposes a public **read-only** `/qa` command in a user's **own private chat**. The feature does not require subscribing to monitoring alerts or granting any admin privileges.

## User experience

- Telegram Bot API **Rich Messages** (Bot API 10.3+) with `sendRichMessage`, `is_rtl: true`, and native `<details><summary>…</summary>…</details>` blocks.
- 23 individually collapsible questions in three sections: passenger cargo (14), Advertio-coordinated document delivery (8), other Advertio services (1).
- All questions start **collapsed** and can be expanded/collapsed in the Telegram client without callbacks or additional messages.
- Answers contain Persian rich HTML, direct support and advertising bot links, and a closing action row.
- Content is maintained in `monitoring/qa.py` (`FAQ_SECTIONS`). It is distinct from the operational `/aboutus` directory.

## Security

Only an actual user's own non-forwarded, private-chat `/qa` request is handled. Group requests and messages where `chat.id != from.id` are ignored. The existing admin allowlist and monitoring commands remain unchanged. No database access or private customer data is used.

## Content policy

The FAQ distinguishes the P2P passenger cargo marketplace from the coordinated document service; it does **not** guarantee deliveries, apply a fixed marketplace shipping price, or publish unconfirmed document-shipping fees. The support team confirms prices and final service terms.

## Test & rollout

```bash
python -m pytest -q tests/test_telegram_qa.py tests/test_telegram_aboutus.py tests/test_telegram_monitor_access.py
```

After deployment/restart, Telegram command registration adds `/qa` to both default and all-private-chat command scopes. Test from a non-admin personal Telegram account using `/qa`. Verify that the native Rich Message appears with collapsible sections on a compatible Telegram client.

**Note:** Rich Message rendering depends on Telegram Bot API/client support. No Telegram API call is executed by repository tests.