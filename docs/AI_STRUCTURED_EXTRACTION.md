# Structured extraction and malformed JSON recovery

Repeated Cloudflare invalid Unicode escapes/unterminated strings and Groq
`json_validate_failed` errors indicate invalid model output, not Advertio failure.
These logs alone do not establish whether a token cap caused truncation.

## Generation

Groq extraction using `openai/gpt-oss-20b` or `openai/gpt-oss-120b` now sends
`response_format.type=json_schema` with `strict=true`, a closed category-specific
shape derived from `CATEGORY_FIELDS`, and nullable values for every field.
Field value types remain compatible with the permissive legacy internal model;
this is a generation constraint, not a replacement for local factual validation
or Advertio's Jobs schema/catalog checks. Unknown fields and category changes are
not requested. Other models retain `json_object`. Classification is unchanged.
GPT-OSS uses `reasoning_effort=low`, still omits reasoning from the response, and
retains the operator's configured output token cap. A `finish_reason=length`
response is rejected even if the returned fragment can be parsed as JSON.

All extraction prompts ask for literal Unicode (including Persian), correct JSON
string escaping and complete output. Cloudflare's existing bounded JSON retry
adds a fresh regeneration instruction; it never edits the original advertisement,
repairs malformed JSON heuristically, or accepts a truncated record. Retry counts
and failover policy remain unchanged. Recoverable Groq JSON generation errors log
at WARNING during retries; exhausted records still produce the existing ERROR
and persist `invalid_provider_output` for explicit retry from Back Office.

Cloudflare extraction also sends an explicit nested output template derived from
`CATEGORY_FIELDS`. A parseable response with missing/null/scalar category data,
an ambiguous array, or the wrong category is regenerated through the same
`TELCLAW_CLOUDFLARE_INVALID_JSON_MAX_RETRIES` budget as malformed JSON (default 1,
maximum 2). Syntax and structure failures do not receive separate retry budgets.
Existing singleton-object array normalization remains supported. Null, empty or
multi-record arrays are not converted into successful empty listings. Every
regeneration passes through the rate limiter and all extraction validation;
exhaustion still raises `invalid_provider_output`, allowing the existing alternate
provider fallback and failed-row retry workflow. Logs do not include raw output.

## Operator steps

Deploy the feature commit and restart the application. After the running pipeline
finishes, use Operations → AI Extraction → Retry Invalid AI Extraction, then Run AI
Extraction. This retries all failed rows with that exact reason, not successes.
If truncation persists, inspect the effective `TELCLAW_GROQ_MAX_COMPLETION_TOKENS`
(default 1200) and consider a controlled increase (for example 4096), accounting
for request/token budgets. This change does not automatically raise that cap.
No new settings, migrations or automatic historical reprocessing are introduced.
No live provider call or production extraction was performed during development;
mocked tests cannot prove a particular model/deployment will always succeed.
Rollback by reverting the feature commit; saved records and queue states remain.

Reference: https://console.groq.com/docs/structured-outputs and
https://console.groq.com/docs/api-reference (verified 2026-10-25).
