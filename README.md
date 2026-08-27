# Telclaw

Telclaw is a Telegram data-processing pipeline. Its purpose is to collect information from selected Telegram sources, clean and normalize the data, prepare structured datasets for category-based AI processing, validate AI results, and eventually deliver approved structured outputs to downstream services.

## System Goals

Telclaw is being developed around the following business and technical goals:

1. **Collect Telegram data** from configured channels/groups using managed Telegram sessions.
2. **Preserve raw information** so the original collected data remains available for reprocessing when cleaning rules or AI logic change.
3. **Clean and normalize data** using deterministic processing such as text cleaning, metadata normalization, duplicate detection, validation, and noise removal.
4. **Create a structured dataset** that is consistent and suitable for automated processing and analysis.
5. **Classify data** using configurable rules/categories before AI processing where deterministic classification is sufficient.
6. **Process categorized data with AI** using category-specific parameters, prompts, and output schemas to extract the information required by the business.
7. **Validate AI output** before it is accepted as processed data. Invalid or low-confidence results must be retryable or rejectable rather than silently passing through.
8. **Apply business rules** to determine which processed records should continue to downstream delivery.
9. **Deliver structured results** to external services through reliable APIs/integrations with retry, timeout, status tracking, and error handling.
10. **Maintain traceability** for every record across the pipeline: collected → cleaned → classified → AI processed → validated → delivered.

The current development focus is the first part of this pipeline: **collection, cleaning/normalization, and reliable storage**. AI processing and downstream delivery are planned as separate layers and should not be tightly coupled to the crawler.

## End-to-End Pipeline

```text
Telegram
   ↓
Collection
   ↓
Raw Storage
   ↓
Cleaning
   ↓
Normalization
   ↓
AI Category Classification
   ↓
Category-specific AI Extraction
   ↓
Validation
   ↓
Category Table
   ↓
Business Rules
   ↓
Delivery
   ↓
External Service
```

Each stage should have a clear responsibility, a defined input/output contract, error handling, and a persisted processing status where appropriate.

## System Structure

The target architecture is organized into independent layers:

```text
Telclaw/
├── main.py                         # Application entry point
├── config.py                       # Environment/runtime configuration
├── ui.py                           # Console interaction layer
│
├── collection/
│   ├── crawler.py                  # Telegram data collection
│   ├── channel_manager.py          # Source/channel configuration
│   └── ...
│
├── telegram/
│   └── sessions_manager.py         # Telegram sessions and clients
│
├── processing/
│   ├── cleaner.py                  # Data cleaning
│   ├── normalizer.py               # Data normalization
│   ├── duplicate_detector.py       # Duplicate detection
│   ├── classifier.py               # Rule/category classification
│   └── validator.py                # Data/result validation
│
├── ai/
│   ├── processor.py                # AI orchestration
│   ├── prompts.py                  # Prompt construction
│   ├── schemas.py                  # Structured AI output schemas
│   └── category_configs.py         # Category-specific parameters
│
├── delivery/
│   ├── client.py                   # External service integration
│   ├── queue.py                    # Delivery jobs/retry handling
│   └── ...
│
├── storage/
│   ├── database.py                 # SQLite access/repositories
│   └── migrations.py               # Database schema migrations
│
└── tests/
    ├── collection/
    ├── processing/
    ├── ai/
    └── delivery/
```

The current repository still contains some of these responsibilities in the existing top-level modules. The structure above is the **target architecture**, to be reached incrementally rather than through a single large rewrite.

## Data Storage Strategy

SQLite is the primary persistence layer for new data.

```text
Telegram
   ↓
Collector
   ↓
SQLite
   ├── raw messages
   ├── cleaned/normalized records
   ├── classifications
   ├── AI results
   ├── processing status
   └── delivery status
```

CSV is not the primary database. Existing legacy CSV files do **not** need to be migrated. If CSV output is needed later, it should be generated as an export from SQLite.

The database must provide deterministic duplicate protection and indexes appropriate for the expected query patterns.

## AI Processing Model

AI processing is configuration-driven. Each category can define:

- category name and description
- required fields
- optional fields
- extraction parameters
- batch classification instructions
- prompt/instructions
- output schema
- validation rules
- confidence threshold
- retry policy

Example:

```text
Category: Job

Required:
- title
- company
- location

Optional:
- salary
- employment_type
- experience
- skills
- deadline

AI output:
JSON matching the category schema
```

AI output must never be sent directly to an external service without validation.

## Development Roadmap

### Phase 1 — Collection Foundation

- Stabilize Telegram session management.
- Separate collection logic from UI logic.
- Store all new collected messages in SQLite.
- Implement reliable duplicate protection.
- Define raw message schema.
- Add indexes for channel/message/date queries.
- Add basic collection tests.

### Phase 2 — Cleaning & Normalization

- Move cleaning into independent processing modules.
- Define a deterministic cleaned-data schema.
- Implement text normalization and metadata normalization.
- Improve duplicate/noise detection.
- Persist processing status and errors.
- Add unit tests for each cleaner.

### Phase 3 — Category & Rule Engine

- Define category configuration files/schema.
- Add rule-based pre-classification.
- Define category-specific required/optional parameters.
- Route records to the correct processing pipeline.

### Phase 4 — AI Processing

- Add an AI provider abstraction so the application is not tied to one model/provider.
- Implement category-specific prompts.
- Enforce structured output schemas.
- Add validation, confidence thresholds, retry, and failure handling.
- Store prompts/model/version/result metadata for traceability.

### Phase 5 — Business Rules & Delivery

- Define rules for accepting/rejecting AI results.
- Build an outbound delivery abstraction.
- Implement authentication, timeout, retry, rate limiting, and delivery status.
- Make delivery idempotent so a record is not sent twice unintentionally.

### Phase 6 — Monitoring & Operations

- Add structured logs.
- Add pipeline metrics.
- Track failures by stage.
- Add reprocessing tools.
- Add operational dashboards/reports.
- Add backup and recovery procedures.

### Phase 7 — Scale & Optimization

- Introduce job queues where required.
- Parallelize independent processing safely.
- Optimize database indexes and batch operations.
- Add retention/archive strategies.
- Move components to separate services only when actual scale requires it.

## Configuration

Telegram API credentials are loaded from environment variables and are never committed to source control.

1. Copy `.env.example` to `.env`.
2. Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
3. Install dependencies.
4. Start Telclaw.

```bash
cp .env.example .env
python -m pip install -r requirements.txt
python main.py
```

For production, environment variables may be supplied directly by the process manager instead of using `.env`.

## Runtime Storage

Crawler messages are stored in `telclaw.db` using SQLite. Session files, the database, generated CSV files, and crawler logs are runtime data and are ignored by Git.

## Security

If a Telegram API hash has ever been committed to a repository, rotate the credential in Telegram before deploying this branch. Removing it from the current source does not remove it from Git history.

## Current Architecture

The current implementation is intentionally smaller than the target architecture:

```text
main.py
  ↓
ConsoleUI
  ↓
Session Manager ──→ Telethon
  ↓
Crawler
  ↓
SQLite
```

The target pipeline and development roadmap above describe where the project is going. New features should be added in a way that moves the code toward those boundaries instead of increasing coupling between UI, crawler, storage, AI, and delivery layers.
