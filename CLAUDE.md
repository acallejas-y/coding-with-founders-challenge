# CLAUDE.md — VidaRx Transaction State Recovery API

Developer guide for AI-assisted work on this project.

## Project Overview

FastAPI backend that recovers payment transactions that timed out and landed in "unknown" state. VidaRx processes ~15,000 transactions/day across Mexico, Colombia, Chile; 8-12% time out.

## Running Locally

```bash
pip install -r requirements.txt
python scripts/generate_test_data.py   # creates vidarx.db with 169 transactions
uvicorn app.main:app --reload          # http://localhost:8000/docs
```

## Running Tests

```bash
pytest                    # all 96 tests
pytest tests/test_normalizer.py -v    # pure unit tests, no DB
pytest tests/test_api.py -v           # integration tests
pytest --tb=short -q      # compact output
```

## Architecture (quick reference)

```
routers/ → services/recovery.py → processors/*.py → services/normalizer.py
                                → services/duplicate.py
```

- **`processors/`** — 4 mock processors, each with different response format
- **`services/normalizer.py`** — maps all 4 formats to canonical schema
- **`services/recovery.py`** — orchestrates query → normalize → persist
- **`services/duplicate.py`** — detects duplicates, scores confidence, classifies type

## Key Business Rules

| State | `recommended_action` | `next_retry_at` |
|---|---|---|
| approved | `fulfill_order` | null |
| declined | `refund_customer` | null |
| pending | `wait_for_settlement` | set per processor |
| unknown | `escalate_to_manual_review` | set per processor |

**Stale transactions** (> 30 days old): always return `escalate_to_manual_review` + `stale_transaction_warning`, regardless of processor response.

**Retry delays by processor:**
- BancoSur: +5 minutes
- MexPay: +1 hour
- AndesPSP: +24 hours
- CashVoucher: +24 hours

## Duplicate Detection Rules

- Window: same `customer_id` + amount ±5% + timestamp ±10 minutes
- Confidence score (0–100): amount match (40/20) + same processor (20) + time gap (30/20/10)
- `duplicate_type`: `accidental_retry` (score≥80, gap<120s, same processor) | `suspected_retry` (score≥60, gap<300s) | `likely_legitimate`

## Adding a New Processor

1. Create `app/processors/<name>.py` extending `BaseProcessor`
2. Add 3 entries to `app/services/normalizer.py`:
   - `STATUS_FIELD_MAP["<name>"] = "<field>"`
   - entries in `NORMALIZED_STATES` for each status value
   - `TIMESTAMP_PARSERS["<name>"] = _parse_timestamp_<name>`
3. Add retry delay to `RETRY_DELAY_SECONDS` in `normalizer.py`
4. Add to `PROCESSOR_MAP` in `services/recovery.py`
5. Add test cases to `tests/test_normalizer.py`

## Common Pitfalls

- **Stale transactions in tests**: `make_txn()` defaults `created_at` to 1 hour ago. Never hardcode a past date like `datetime(2024, ...)` for "fresh" test transactions — it will trigger the 30-day stale check.
- **Duplicate `real_state` confusion**: `find_duplicates` uses `recovered_state or real_state` to determine the effective state of each transaction. If testing "approved + unknown" pairs, explicitly set `real_state="unknown"` on the unknown txn.
- **Async tests**: `pytest.ini` sets `asyncio_mode = auto` — no `@pytest.mark.asyncio` decorator needed.
- **In-memory DB**: The test suite uses `StaticPool` in-memory SQLite. The `reset_db` fixture is `autouse=True` and runs before every test, so DB state never leaks between tests.
- **Processor mocking**: Use `patch.dict(recovery_module.PROCESSOR_MAP, {"<processor>": mock})` — don't patch the processor class directly, as the service holds a singleton instance in `PROCESSOR_MAP`.

## File Map

```
app/
  main.py            FastAPI app + lifespan
  database.py        SQLAlchemy engine + get_db dependency
  models.py          Transaction ORM model
  schemas/
    requests.py      BulkRecoverRequest (validator: max 500 IDs)
    responses.py     RecoverResponse, DuplicateReport, BulkSummary
  processors/
    base.py          Abstract BaseProcessor
    bancosur.py      ISO8601 timestamps, APPROVED/DECLINED/PENDING/UNKNOWN
    mexpay.py        Unix epoch timestamps, success/failed/processing/indeterminate
    andespsp.py      DD/MM/YYYY timestamps, aprobada/rechazada/pendiente/desconocido
    cashvoucher.py   RFC2822 timestamps, PAID/REJECTED/WAITING/ERROR
  services/
    normalizer.py    Format → canonical state + ISO8601 + recommended_action + retry
    recovery.py      Orchestration + stale detection + DB persistence
    duplicate.py     Detection query + confidence score + duplicate_type + recommendation
  routers/
    transactions.py  POST /recover, GET /duplicates
    bulk.py          POST /bulk-recover
scripts/
  generate_test_data.py   Seeds 169 transactions (run before starting API)
tests/
  conftest.py        Fixtures: reset_db, db, client, make_txn helper
  test_normalizer.py Pure unit tests (no DB)
  test_duplicate.py  DB-backed duplicate detection tests
  test_recovery.py   Recovery service tests (processors mocked)
  test_api.py        Integration tests via TestClient
```
