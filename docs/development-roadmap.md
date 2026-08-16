# Telclaw Development Roadmap

The project is developed as independent pipeline stages. Do not implement future stages by coupling them into the collector.

## Phase 1 — Collection Foundation (current)

- Telegram session/client boundary
- Channel/source configuration
- Collect new Telegram messages
- Preserve raw message content
- Deterministic cleaning and normalization
- SQLite as the source of truth
- Idempotent `(channel_username, message_id)` storage
- Processing status and pipeline version
- Collection/error logging

**Exit criteria:** a new Telegram message can be collected, stored once, and inspected with both its raw and cleaned content without CSV being involved.

## Phase 2 — Processing Pipeline

- Separate processing jobs from collection
- Add richer cleaning rules
- Add normalization/extraction helpers
- Add duplicate/noise detection as explicit processing stages
- Persist stage status and errors
- Add reprocessing of stored raw records

**Exit criteria:** cleaning rules can change without recrawling Telegram.

## Phase 3 — Category and Rule Engine

- Separate Telegram source configuration from business categories
- Define category schemas
- Define required/optional fields
- Add deterministic pre-classification
- Route records to category-specific processors

**Exit criteria:** a cleaned record can be assigned to a stable category and a deterministic processing contract.

## Phase 4 — AI Processing

- Provider-agnostic AI interface
- Category-specific prompts
- Structured output schema
- Validation and confidence thresholds
- Retry and failure handling
- Persist model/provider/prompt/schema versions

**Exit criteria:** AI output is machine-validatable and reproducible enough to audit and reprocess.

## Phase 5 — Business Rules and Delivery

- Define publish/send eligibility rules
- Build external-service adapter boundary
- Implement Advertio Ingest API client
- Upload media before lead submission
- Enforce source cohort rules
- Handle idempotency, retry, timeout, and rate/concurrency controls
- Deactivate/remove stale source records when required

**Exit criteria:** only validated, eligible records are sent to the external service and every delivery is traceable.

## Phase 6 — Operations

- Structured logs
- Metrics by pipeline stage
- Failed-job inspection
- Safe reprocessing tools
- Health checks
- Database backup/recovery
- Operational dashboard

## Phase 7 — Scale

Only after measured need:

- Queue-based processing
- Parallel workers
- Batch database operations
- Separate services
- Larger database/managed storage if SQLite becomes a real bottleneck

Do not introduce distributed infrastructure before the single-process pipeline is observable and correct.
