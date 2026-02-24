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
| declined | `refund_customer` |
| pending | `wait_for_settlement` |
| unknown | `escalate_to_manual_review` |

**Stale transactions** (> 30 days old) always return `escalate_to_manual_review` with a `stale_transaction_warning` message, regardless of what the processor reports.

**Retry schedule** (`next_retry_at` field, set for pending/unknown):
| Processor | Next retry |
|---|---|
| BancoSur | +5 minutes |
| MexPay | +1 hour |
| AndesPSP | +24 hours |
| CashVoucher | +24 hours |

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
  "duplicates_found": 1,
  "duplicates": [
    {
      "duplicate_transaction_id": "txn_def456",
      "confidence_score": 90,
      "duplicate_type": "accidental_retry",
      "time_gap_seconds": 38,
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

## Demo: 3 API Calls

### 1. Single Transaction Recovery

```bash
curl -X POST http://localhost:8000/api/v1/transactions/txn_demo_a1/recover
```

```json
{
    "transaction_id": "txn_demo_a1",
    "original_status": "unknown",
    "recovered_state": "approved",
    "processor_timestamp": "2026-02-24T22:12:50.938516+00:00",
    "recommended_action": "fulfill_order",
    "next_retry_at": null,
    "stale_transaction_warning": null,
    "processor_raw_response": {
        "transaction_id": "txn_demo_a1",
        "status": "APPROVED",
        "timestamp": "2026-02-24T22:12:50.938516+00:00",
        "processor": "BancoSur",
        "authorization_code": "BS961945",
        "response_code": "00"
    },
    "recovered_at": "2026-02-24T22:12:50.938560+00:00"
}
```

The BancoSur raw response (`status: "APPROVED"`) is normalized to `recovered_state: "approved"` and mapped to `recommended_action: "fulfill_order"`. The finance team can now ship the order.

---

### 2. Duplicate Detection in Action

Two transactions from the same customer, same amount (MXN 3,200), 38 seconds apart — a classic panic retry.

```bash
# First recover both transactions, then check for duplicates
curl -X POST http://localhost:8000/api/v1/transactions/txn_demo_b1/recover
curl -X POST http://localhost:8000/api/v1/transactions/txn_demo_b2/recover
curl http://localhost:8000/api/v1/transactions/txn_demo_b1/duplicates
```

```json
{
    "transaction_id": "txn_demo_b1",
    "duplicates_found": 1,
    "duplicates": [
        {
            "duplicate_transaction_id": "txn_demo_b2",
            "confidence_score": 90,
            "duplicate_type": "accidental_retry",
            "time_gap_seconds": 38.0,
            "recommendation": "refund_duplicate",
            "reasoning": "Both approved. Keep txn_demo_b1 (earlier). Refund txn_demo_b2."
        }
    ]
}
```

Confidence score 90/100: exact amount (+40) + same processor (+20) + gap under 2 min (+30). Classified as `accidental_retry`. Recommendation: keep the first charge, refund the second.

---

### 3. Bulk Recovery — Summary Report (173 transactions)

```bash
curl -X POST http://localhost:8000/api/v1/transactions/bulk-recover \
  -H "Content-Type: application/json" \
  -d '{"transaction_ids": ["txn_1", "txn_2", ..., "txn_173"]}'
```

```json
{
    "total_processed": 173,
    "results": {
        "approved": 113,
        "declined": 31,
        "pending": 10,
        "still_unknown": 18,
        "errors": 1
    },
    "duplicates_detected": 30,
    "total_recommended_refund_amount": 148953.14,
    "refund_currency_breakdown": {
        "MXN": 39170.95,
        "CLP": 44708.59,
        "COP": 65073.60
    },
    "failed_transactions": [
        {
            "transaction_id": "txn_29265486",
            "error": "AndesPSP: error de conexión"
        }
    ],
    "processing_time_ms": 278,
    "transactions": ["... 172 individual results ..."]
}
```

173 transactions processed concurrently in **278ms**. 1 processor error isolated without aborting the batch. 30 duplicate pairs detected across the dataset with MXN 148,953 in recommended refunds broken down by currency.

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
