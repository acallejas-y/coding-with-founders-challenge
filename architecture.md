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

## Handling Processor Timeouts

The original problem is that processors didn't respond within VidaRx's 10-second threshold, leaving transactions in `status="unknown"`. This API solves the _post-hoc_ recovery problem: after a timeout has already occurred, query the processor to find out what actually happened.

**How we handle timeouts in the recovery mocks:**
- Each processor mock simulates realistic latency (10–200ms) using `asyncio.sleep()`
- Mocks have a ~5% random error rate (503 responses) to simulate real-world processor unreliability
- The single recovery endpoint propagates processor errors as HTTP 502 (Bad Gateway) so the caller knows the processor was unreachable — not that the transaction was declined
- The bulk endpoint isolates these errors per transaction: a 503 from one processor does not block the other 499 recoveries
- For `pending` and `unknown` outcomes, the response includes `next_retry_at` — a calculated timestamp for when to attempt recovery again based on processor type (BancoSur: +5 min, MexPay: +1 hour, AndesPSP/CashVoucher: +24 hours)

**Why not just retry immediately?** Each processor has different settlement windows. BancoSur resolves quickly; AndesPSP processes cash vouchers in batches overnight. Retrying a cash voucher every 5 minutes wastes requests and stays within processor rate limits.

## Improvements With More Time

1. **Persistent retry queue**: Instead of returning `next_retry_at` in the response and relying on the caller to re-invoke, a background worker (Celery + Redis or APScheduler) would automatically re-attempt recovery at the scheduled time and notify VidaRx's finance system when a transaction resolves.

2. **Webhook callbacks**: Rather than polling, integrate processor webhooks so recoveries happen in real time. The mock framework already has the normalizer pattern in place — adding a `POST /webhook/{processor}` endpoint would take 1–2 hours.

3. **Confidence scoring for duplicate detection using payment method**: The current scoring uses amount, processor, and time gap. Adding payment method (card BIN, wallet ID) as a fourth factor (+15 pts) would reduce false positives where two different customers happen to use the same processor for the same amount within 10 minutes.

4. **Audit log table**: Currently `recovered_state` overwrites in place. A separate `recovery_attempts` table would track every query attempt (timestamp, raw response, outcome), which is required for PCI-DSS compliance in a production pharmacy system.

5. **Rate limiting per processor**: BancoSur and MexPay have rate limits. A token bucket per processor instance would prevent bulk recovery jobs from triggering 429s and getting the VidaRx IP blocked.

6. **Database upgrade**: SQLite works for this demo but would not handle concurrent writes from multiple API workers. PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED` would enable safe concurrent recovery without race conditions on the same transaction ID.
