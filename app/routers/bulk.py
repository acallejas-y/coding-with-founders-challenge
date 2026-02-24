import asyncio
import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.requests import BulkRecoverRequest
from app.schemas.responses import BulkSummary, BulkResultCounts, RecoverResponse, FailedTransaction
from app.services.recovery import recover_transaction
from app.services.duplicate import find_duplicates
from app import models

router = APIRouter()


async def _recover_one(transaction_id: str, db: Session):
    """Attempt recovery for one transaction; return (result_or_none, error_or_none)."""
    try:
        result = await recover_transaction(transaction_id, db)
        return result, None
    except Exception as e:
        return None, str(e)


@router.post("/bulk-recover", response_model=BulkSummary)
async def bulk_recover(request: BulkRecoverRequest, db: Session = Depends(get_db)):
    """
    Recover up to 500 timed-out transactions concurrently.

    Uses asyncio.gather() for concurrent processing.
    Partial failures are isolated — one error does not abort the batch.

    Returns a summary report with per-state counts, duplicate detection results,
    and total recommended refund amounts.
    """
    start_ms = time.time() * 1000

    # Run all recoveries concurrently
    tasks = [_recover_one(txn_id, db) for txn_id in request.transaction_ids]
    outcomes = await asyncio.gather(*tasks)

    counts = BulkResultCounts()
    transaction_responses = []
    failed_transactions = []
    total_refund = 0.0
    refund_breakdown: dict = {}
    duplicates_detected = 0

    for txn_id, (result, error) in zip(request.transaction_ids, outcomes):
        if error:
            counts.errors += 1
            failed_transactions.append(FailedTransaction(
                transaction_id=txn_id,
                error=error,
            ))
            continue

        # Tally state counts
        state = result.recovered_state
        if state == "approved":
            counts.approved += 1
        elif state == "declined":
            counts.declined += 1
        elif state == "pending":
            counts.pending += 1
        else:
            counts.still_unknown += 1

        # Build response object
        transaction_responses.append(RecoverResponse(
            transaction_id=result.transaction_id,
            original_status=result.original_status,
            recovered_state=result.recovered_state,
            processor_timestamp=result.processor_timestamp,
            recommended_action=result.recommended_action,
            next_retry_at=result.next_retry_at,
            stale_transaction_warning=result.stale_transaction_warning,
            processor_raw_response=result.processor_raw_response,
            recovered_at=result.recovered_at,
        ))

        # Duplicate detection + refund tallying
        try:
            dups = find_duplicates(result.transaction_id, db)
            if dups:
                duplicates_detected += len(dups)
                # Tally refund amounts for refund_duplicate recommendations
                txn = db.query(models.Transaction).filter(
                    models.Transaction.id == result.transaction_id
                ).first()
                if txn:
                    for dup in dups:
                        if dup.recommendation == "refund_duplicate":
                            total_refund += txn.amount
                            currency = txn.currency
                            refund_breakdown[currency] = (
                                refund_breakdown.get(currency, 0.0) + txn.amount
                            )
        except Exception:
            pass

    elapsed_ms = int(time.time() * 1000 - start_ms)

    return BulkSummary(
        total_processed=len(request.transaction_ids),
        results=counts,
        duplicates_detected=duplicates_detected,
        total_recommended_refund_amount=round(total_refund, 2),
        refund_currency_breakdown=refund_breakdown,
        transactions=transaction_responses,
        failed_transactions=failed_transactions,
        processing_time_ms=elapsed_ms,
    )
