# AI Processing Contract

This document defines the future AI boundary. The collector must not call an AI provider directly.

## Input

The AI layer receives a cleaned, normalized record plus a category definition.

```text
Cleaned Record
   + Category
   + Parameters
   + Output Schema
   + Validation Rules
        ↓
      AI Processor
```

## Category definition

A category may define:

- `slug`
- description
- required fields
- optional fields
- field types/enums
- extraction instructions
- prompt template/version
- output schema version
- confidence threshold
- retry policy

## Output

AI output must be structured data matching the category schema. Free-form text is not a valid downstream contract.

Every result should retain:

- source record ID
- category
- provider/model
- prompt version
- schema version
- extracted payload
- confidence when available
- validation status
- processing timestamp
- error/retry information when applicable

## Validation

AI output is untrusted input. It must pass schema and business validation before becoming `validated` or entering delivery.

```text
AI result
   ↓
Schema validation
   ↓
Field validation
   ↓
Business validation
   ├── invalid → retry/reject
   └── valid   → validated
```

## Reprocessing

AI results must be versioned so a changed prompt, category definition, or model can process existing cleaned records again without collecting them from Telegram.
