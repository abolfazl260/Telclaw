# Admin-forwarded Telegram message import

The existing Telclaw monitoring bot accepts **forwarded messages sent directly to
its private chat by an authorized administrator**. It does not open a new public
bot endpoint and does not change existing commands.

## Import rules

- Must be a Telegram Bot API forward (`forward_origin` or supported legacy
  forward metadata), not ordinary text containing the word "Forwarded".
- Forwarded **text or caption must contain more than 10 whitespace-delimited
  words**; 10 exactly is rejected.
- The ad text must identify **one unambiguous Telegram contact username**,
  using `@username`, a direct `t.me/username` profile URL, or an explicit
  `Username: @username` / `یوزرنیم: @username` label.
  A labeled contact takes precedence over other incidental mentions; conflicting
  labeled contacts or multiple unlabeled contacts are rejected.
- Only messages sent by `ADMIN_USER_IDS` from the admin's own private chat
  are handled. Other users and group forwards are ignored.
- The Bot API must provide the original forwarded message date. The system
  **does not use the current import date as a substitute** to avoid making
  old ads appear new.

## Database and processing

The importer creates a **normal collected message** in the existing
`messages` table (no additional schema migration or extra content store):

| Field | Saved value |
| --- | --- |
| `channel_username` | `admin-forward:<admin Telegram user ID>` (internal namespace) |
| `message_id` | Bot API private-chat message ID of the forwarded message |
| `raw_text`, `text` | Original forwarded text/caption |
| `cleaned_text` | NULL until normal cleaning |
| `date` | Original Telegram forward date, UTC |
| `sender_username` | Contact username extracted from text (without @) |
| `sender_id` | NULL; no invented Telegram user identity |
| `sender_is_original` | 0 (the admin is not the verified original author) |
| `sender_type` | `admin_forward_text` (explicit provenance marker) |
| `message_link` | NULL; never fabricate a public link to the admin's private chat |
| `processing_status`, `ai_status` | `pending`, `waiting` |

The record automatically joins the **existing** Processing → Classification →
AI Extraction flow when the ordinary pipeline next runs. The handler replies
with the import result but never invokes AI or creates an Advertio listing
directly. Normal eligibility, data quality, date windows and publication gates
continue to apply.

Accepted imports are fingerprinted using the existing normalized text
`content_hash`. Exact repeated forwards (including repeat imports by a
different administrator) are skipped. The original raw content is preserved.

## Contact provenance

The text-supplied username is **not proof of authorship** and must not be used
to infer a Telegram numeric user ID. Housing and Cargo may use it as a contact
handle; Jobs accepts it as a text contact only for records marked
`admin_forward_text`. Jobs never sets `posterTelegramUserId` for these records.
The internal namespace is intentionally **not** a valid Telegram username:
it cannot be turned into a fabricated `t.me/<channel>/<messageId>` post URL.

For example, an administrator may forward a message containing:

```text
Hiring an experienced warehouse worker in Toronto for a full-time shift.
Our team is recruiting immediately; please review the responsibilities
and contact the original posting account for details.
Username: @Original_Poster
```

The imported sender handle will be `Original_Poster`, while the bot
administrator remains the authenticated submitter and is not substituted into
the advertisement's contact.

## Validation

After review, run:

```bash
python -m pytest -q tests/test_admin_forward_ingest.py tests/test_advertio_jobs.py
python -m pytest -q tests
```

Then forward one fresh test message from an authorized private chat, inspect
the recorded `messages` row and subsequent classification/extraction/Advertio
status. Do not assume that saving a forwarded message guarantees publication.
