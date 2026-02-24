# VidaRx Transaction State Recovery API

A FastAPI backend service that recovers payment transactions that timed out and landed in an "unknown" state. VidaRx pharmacy processes ~1,200–1,800 unknown-state transactions daily (8-12% of volume), causing double-charges, unnecessary refunds, and compliance risk.

## Problem Statement

When a payment request times out before the processor responds, the transaction is recorded as "unknown". This creates three problems:
1. **Double charges**: Customer retries, processor already charged them
2. **Unnecessary refunds**: Finance team refunds transactions that were actually declined
3. **Compliance risk**: Unresolved transaction states in audit logs

## Solution

This service queries payment processors post-timeout, normalizes heterogeneous responses, detects duplicates, and exposes recovery results via a REST API.

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Framework | FastAPI (Python 3.9) | Auto Swagger docs, Pydantic validation, async |
| Database | SQLite + SQLAlchemy | Zero config, enables duplicate queries |
| Validation | Pydantic v1 | Response normalization schemas |
| Async | asyncio.gather() | Concurrent bulk processing |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Seed the database (150+ transactions)
python scripts/generate_test_data.py

# 3. Start the API
uvicorn app.main:app --reload

# 4. Open Swagger UI
open http://localhost:8000/docs
```

## API Endpoints

### POST `/api/v1/transactions/{transaction_id}/recover`

Recovers a single timed-out transaction by querying its payment processor.

**Response:**
```json
{
  "transaction_id": "txn_abc123",
  "original_status": "unknown",
  "recovered_state": "approved",
  "processor_timestamp": "2024-01-15T10:23:45Z",
  "recommended_action": "fulfill_order",
  "processor_raw_response": {"status": "APPROVED", "authorization_code": "BS123456"},
  "recovered_at": "2024-01-15T14:00:00Z"
}
```

**Recommended actions:**
| State | Action |
|---|---|
| approved | `fulfill_order` |
| declined | `notify_customer_payment_failed` |
| pending | `wait_and_retry` |
| unknown | `manual_review_required` |

### GET `/api/v1/transactions/{transaction_id}/duplicates`

Finds duplicate transactions using:
- Same `customer_id`
- Amount within ±5%
- Timestamp within ±10 minutes

**Confidence scoring (0–100):**
| Factor | Score |
|---|---|
| Exact amount match | +40 |
| Amount within 5% | +20 |
| Same processor | +20 |
| Time gap < 2 min | +30 |
| Time gap < 5 min | +20 |
| Time gap < 10 min | +10 |

**Response:**
```json
{
  "transaction_id": "txn_abc123",
  "duplicates_found": 2,
  "duplicates": [
    {
      "duplicate_transaction_id": "txn_def456",
      "confidence_score": 95,
      "time_gap_seconds": 45,
      "recommendation": "refund_duplicate",
      "reasoning": "Both approved. Keep txn_abc123 (earlier). Refund txn_def456."
    }
  ]
}
```

### POST `/api/v1/transactions/bulk-recover`

Recovers up to 500 transactions concurrently using `asyncio.gather()`. Partial failures are isolated.

**Request:**
```json
{
  "transaction_ids": ["txn_1", "txn_2", "txn_3"]
}
```

**Response:**
```json
{
  "total_processed": 150,
  "results": {
    "approved": 89,
    "declined": 38,
    "pending": 14,
    "still_unknown": 7,
    "errors": 2
  },
  "duplicates_detected": 18,
  "total_recommended_refund_amount": 45230.50,
  "refund_currency_breakdown": {"MXN": 12000, "COP": 28000000, "CLP": 95000},
  "transactions": [...],
  "processing_time_ms": 1240
}
```

## Mock Processors

Each processor uses different field names and status vocabularies to test normalization:

| Processor | Status Field | Status Values | Timestamp Format |
|---|---|---|---|
| BancoSur | `status` | `APPROVED / DECLINED / PENDING / UNKNOWN` | ISO8601 |
| MexPay | `payment_status` | `success / failed / processing / indeterminate` | Unix epoch |
| AndesPSP | `transaction_state` | `aprobada / rechazada / pendiente / desconocido` | `DD/MM/YYYY HH:MM:SS` |
| CashVoucher | `state` | `PAID / REJECTED / WAITING / ERROR` | RFC2822 |

All mocks simulate 10–200ms latency and ~5% error rate.

## Test Data

The seed script generates 150+ transactions:
- **Distribution**: 60% approved, 25% declined, 10% pending, 5% unknown
- **15–20 duplicate clusters**: accidental retries and legitimate same-price pairs
- **Edge cases**: stale transactions (>30 days), USD currency mismatches, null customer IDs

## Demo Walkthrough

```bash
# Get a transaction ID from the database
sqlite3 vidarx.db "SELECT id FROM transactions LIMIT 5;"

# Recover a single transaction
curl -X POST http://localhost:8000/api/v1/transactions/txn_XXXXXXXX/recover

# Check for duplicates
curl http://localhost:8000/api/v1/transactions/txn_XXXXXXXX/duplicates

# Bulk recover all unknown transactions
sqlite3 vidarx.db "SELECT json_group_array(id) FROM transactions WHERE status='unknown';" | \
  xargs -I{} curl -X POST http://localhost:8000/api/v1/transactions/bulk-recover \
    -H "Content-Type: application/json" \
    -d '{"transaction_ids": {}}'
```

## Project Structure

```
vidarx-recovery-api/
├── app/
│   ├── main.py                 # FastAPI app + lifespan (DB init + seed)
│   ├── database.py             # SQLAlchemy engine, session, Base
│   ├── models.py               # Transaction ORM model
│   ├── schemas/
│   │   ├── requests.py         # BulkRecoverRequest
│   │   └── responses.py        # RecoverResponse, DuplicateReport, BulkSummary
│   ├── routers/
│   │   ├── transactions.py     # POST /recover, GET /duplicates
│   │   └── bulk.py             # POST /bulk-recover
│   ├── services/
│   │   ├── recovery.py         # Orchestrates processor query + normalization
│   │   ├── duplicate.py        # Duplicate detection + confidence scoring
│   │   └── normalizer.py       # Maps each processor format → standard schema
│   └── processors/
│       ├── base.py             # Abstract base processor
│       ├── bancosur.py         # BancoSur Gateway mock
│       ├── mexpay.py           # MexPay mock
│       ├── andespsp.py         # AndesPSP mock
│       └── cashvoucher.py      # CashVoucher mock
├── scripts/
│   └── generate_test_data.py   # Generates 150+ transactions + seeds SQLite
├── requirements.txt
├── README.md
└── architecture.md
```
