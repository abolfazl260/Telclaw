# Telclaw Architecture

## Purpose

Telclaw is a staged data pipeline, not a single Telegram crawler. The system collects Telegram data, preserves the source record, cleans and normalizes it, classifies it, sends eligible records to an AI processor using category-specific schemas, validates the result, applies business rules, and finally delivers approved records to an external service.

## Boundaries

```text
Telegram
   │
   ▼
Collection ──────────────── Telegram I/O only
   │
   ▼
Raw Storage ─────────────── Preserve source data
   │
   ▼
Processing ──────────────── Cleaning / normalization / deterministic rules
   │
   ▼
Category Engine ─────────── Select processing definition
   │
   ▼
AI ──────────────────────── Structured extraction / classification
   │
   ▼
Validation ──────────────── Reject or retry invalid results
   │
   ▼
Business Rules ──────────── Decide what is publishable
   │
   ▼
Delivery ───────────────── External API integration
```

Each boundary should have a small, explicit contract. A module must not reach across several boundaries just because the current implementation is small.

## Current target boundaries

- `collection/`: Telegram collection and Telegram-specific extraction.
- `processing/`: deterministic cleaning and normalization.
- `storage/`: SQLite persistence and schema management.
- `ai/`: future AI provider abstraction, prompts, category schemas, and AI result handling.
- `delivery/`: future external-service clients, retry, idempotency, and delivery status.
- `docs/integrations/`: contracts for external systems such as Advertio.

The console UI remains an orchestration interface. It must not contain database, AI, or external API implementation details.

## Data lifecycle

Every message should be traceable through explicit states. The first implementation stores both `raw_text` and `cleaned_text` in SQLite so a future cleaning rule can be evaluated without losing the source message.

```text
collected_cleaned
       ↓
classified
       ↓
ai_processed
       ↓
validated
       ↓
ready_for_delivery
       ↓
delivered
```

Failure states should identify the stage that failed and retain enough metadata to retry safely.

## Storage principle

SQLite is the primary store for new data. CSV is not part of the active collection pipeline. Legacy CSV files do not need to be imported. If an export is required, it should be produced from SQLite.

## Important separation

Telegram source categories and AI business categories are different concepts. A source configuration may say which Telegram channels to crawl; an AI category defines what information to extract from a cleaned record. They should not become one configuration object as the system grows.

## Non-goals of the current phase

The collection foundation must not depend on an AI provider or Advertio. Those integrations are later pipeline stages and must be replaceable without rewriting Telegram collection.
