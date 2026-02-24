# VidaRx Recovery API — Architecture

## System Overview

```
Client Request
     │
     ▼
┌─────────────────────────────────────┐
│           FastAPI Application        │
│  POST /recover                       │
│  GET  /duplicates                    │
│  POST /bulk-recover                  │
└──────────────┬──────────────────────┘
               │
       ┌───────▼───────┐
       │ Recovery       │
       │ Service        │
       │ (recovery.py)  │
       └───────┬────────┘
               │
    ┌──────────▼──────────────────┐
    │   Processor Router          │
    │   bancosur / mexpay /       │
    │   andespsp / cashvoucher    │
    └──────────┬──────────────────┘
               │ raw response (different formats)
    ┌──────────▼──────────────────┐
    │   Normalizer Service        │
    │   (normalizer.py)           │
    │   → canonical state + ISO ts │
    └──────────┬──────────────────┘
               │
    ┌──────────▼──────────────────┐
    │   SQLite Database           │
    │   (persist recovered_state) │
    └─────────────────────────────┘
```

## Key Design Decisions

### 1. Idempotent Recovery

If a transaction has already been recovered (has a `recovered_state`), the API returns the cached result immediately without re-querying the processor. This prevents:
- Double-recovery charges to the processor API (rate limits)
- State inconsistency if a second recovery returns a different result

### 2. Normalizer Pattern

Each processor uses completely different field names, status vocabularies, and timestamp formats. Rather than scattering processor-specific logic throughout the codebase, a single `normalizer.py` module handles all mappings. Adding a new processor requires only:
1. A new processor mock in `processors/`
2. Three new entries in `normalizer.py` (status field, status map, timestamp parser)

### 3. Bulk Processing with Error Isolation

`asyncio.gather()` runs all recovery tasks concurrently. Each task is wrapped in a try/except so a single processor 503 does not abort the batch. The response distinguishes `errors` from `still_unknown` so the finance team knows which transactions need manual retry.

### 4. Duplicate Detection Strategy

The duplicate query uses a compound filter: same `customer_id` + amount within ±5% + timestamp within ±10 minutes. The ±5% tolerance handles currency conversion rounding that sometimes causes near-identical amounts.

Confidence scoring weights time proximity heavily (30 points) because a 30-second gap is a strong signal of a retry, while a 9-minute gap could be a coincidence.

### 5. Recommendation Logic

The recommendation engine handles 5 distinct state-pair outcomes:

| Target State | Candidate State | Recommendation |
|---|---|---|
| approved | approved | `refund_duplicate` (keep earlier) |
| approved | unknown | `mark_as_duplicate` |
| unknown | approved | `mark_as_duplicate` |
| declined | declined | `no_action` |
| approved | declined | `no_action` |
| other | other | `manual_review` |

The key insight: if both are declined, no money was actually collected — no refund needed. Only approve+approve creates an actual double-charge scenario.

## Data Model

```sql
CREATE TABLE transactions (
    id              TEXT PRIMARY KEY,      -- txn_xxxxxxxx
    customer_id     TEXT,                  -- nullable (edge case)
    amount          REAL NOT NULL,
    currency        TEXT(3) NOT NULL,      -- MXN, COP, CLP
    processor       TEXT NOT NULL,         -- bancosur, mexpay, andespsp, cashvoucher
    status          TEXT NOT NULL,         -- always "unknown" (original state)
    real_state      TEXT NOT NULL,         -- ground truth for processor mocks
    created_at      DATETIME NOT NULL,
    recovered_state TEXT,                  -- set after recovery
    recovered_at    DATETIME,              -- set after recovery
    processor_timestamp TEXT,             -- normalized ISO8601 from processor
    notes           TEXT                   -- test metadata
);
```

## Processor Response Normalization

| Processor | Raw Status | Normalized |
|---|---|---|
| BancoSur | APPROVED | approved |
| BancoSur | DECLINED | declined |
| BancoSur | PENDING | pending |
| BancoSur | UNKNOWN | unknown |
| MexPay | success | approved |
| MexPay | failed | declined |
| MexPay | processing | pending |
| MexPay | indeterminate | unknown |
| AndesPSP | aprobada | approved |
| AndesPSP | rechazada | declined |
| AndesPSP | pendiente | pending |
| AndesPSP | desconocido | unknown |
| CashVoucher | PAID | approved |
| CashVoucher | REJECTED | declined |
| CashVoucher | WAITING | pending |
| CashVoucher | ERROR | unknown |

## Error Handling

| Scenario | HTTP Code | Behavior |
|---|---|---|
| Transaction not found | 404 | Returns error detail |
| Transaction not in unknown state | 200 | Returns cached result |
| Processor 503 error | 502 | Surfaces processor error message |
| Bulk: individual error | 200 | Counted in `errors`, batch continues |
| No customer_id | 200 | Returns empty duplicates list |
