# Back-office database assistant

## Use

After deploying this change and restarting Telclaw, open the normal Telegram admin
back-office link, then **AI Assistant** (`/assistant`). Ask in Persian or English:

- چرا Jobs ارسال نمی‌شود؟ چند پیام واقعی را بررسی کن.
- متن اصلی و خروجی AI رکورد 20466 را مقایسه کن.
- Which channels have the most extracted housing ads this week?

The assistant reads the schema and pipeline counts, then may query approved tables
or inspect an individual message. A final answer appears on the same page with an
expandable evidence trail showing queries, row counts, and inspected local IDs.
Follow-up questions use the last six conversation messages. **گفتگوی جدید** clears
the conversation. History stays in page memory and is lost on reload.

No Mac, MCP server process, domain, new key, or MCP SDK is required. The standard
back-office admin session and CSRF checks apply. Analysis uses the configured
`AI_PROVIDER_1`/`AI_PROVIDER_2` priority and their existing Groq/Cloudflare credential
sets and model/token settings. Settings changes affect subsequent requests.
Extraction prompts/queues and delivery eligibility are not modified.

Relevant database text is sent to those AI services and consumes their quota.
The account quota is shared with extraction; analysis has its own process-local
rate limiter. No live provider request was made during implementation.

## Implementation and limits

`ai/database_analyst.py` orchestrates a JSON action loop (`query`, `inspect`,
`answer`) using the existing requests dependency and provider JSON mode. No
provider-specific tool-calling capability or additional SDK is required.
`integrations/database_reader.py` enforces the same read-only table, column,
function, time, and result limits used by the MCP integration.

The model receives only allowlisted non-secret delivery/extraction flags and
window sizes; credentials are used only for provider HTTP authentication.
Configuration/auth/session tables and media references are inaccessible through
SQL. No sending, writing, retries, external URL fetching or automatic corrections
are available. Database content/history are explicitly treated as untrusted data.

Each question permits three database actions and four model rounds, with an overall
six-HTTP-attempt cap across credentials/providers. HTTP timeouts are at most 20
seconds per attempt within a 90-second request budget. Local database operations,
rate-limit waits and network read behavior can add overhead. One analysis can run
per back-office application instance; a second request receives a busy message.
Queries return at most 20 rows to the model, and individual context chunks are
bounded. Large datasets may require narrower questions. Results can change while
the crawler runs. A model answer is an interpretation, not verified ground truth;
check the cited records and evidence trail before taking operational action.

Transport, malformed output and unavailable-provider failures produce a sanitized
error. The assistant does not log response bodies or credentials. It does not
silently claim success when it exhausts its action/request budget.

## Validation and rollback

Tests cover source/extraction comparison, joins, rejected destructive model SQL,
conversation validation, concurrency/step limits, both provider request formats,
fallback, session/CSRF enforcement, and safe text rendering. Existing MCP reader
and protocol tests continue to run. Stop using `/assistant` or revert this change
to roll back; no migration or persistent conversation data is introduced.

## Incomplete-analysis recovery

The six-attempt and 90-second total budgets are unchanged. Planning reserves two
HTTP attempts and 25 seconds for final synthesis, and switches to synthesis early
when time/attempts are running low. Provider credential attempts are interleaved
so an alternate provider can be reached before exhausting one provider's keys.
Credentials returning 401/403/404/429/5xx are skipped for the rest of that question.
Compact schema/history/tool contexts reduce input size.

If the model still cannot answer, the page explicitly labels the result incomplete
and preserves the actual pipeline counts and successful tool results. It does not
claim the fallback is an AI diagnosis. The evidence panel includes sanitized
provider/phase/attempt/reason codes (HTTP status, malformed output, token limit),
never provider response bodies, tokens, or credentials. Time-limit and HTTP-attempt
failures have distinct messages. This makes a quota/auth/model problem visible
instead of attributing every failure to a question being too broad.

## Direct query results

Every successful SQL query now appears in an open **ردیف‌های واقعی دیتابیس** table
above the model's answer. Column names, original returned cell values and NULLs
come directly from the reader evidence, without model rewriting or summarization.
The table supports horizontal scrolling and preserves text line breaks. A JSON
download contains the exact bounded query result, including column/row arrays and
truncation metadata, so admins can share examples for debugging. Empty results
show zero rows; reader truncation is explicitly flagged. Existing read-only,
session and CSRF restrictions are unchanged. Downloading does not call AI again.
