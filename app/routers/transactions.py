from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.responses import RecoverResponse, DuplicateReport
from app.services.recovery import recover_transaction
from app.services.duplicate import find_duplicates

router = APIRouter()


@router.post("/{transaction_id}/recover", response_model=RecoverResponse)
async def recover(transaction_id: str, db: Session = Depends(get_db)):
    """
    Recover a single timed-out transaction by querying its payment processor.

    - Looks up the transaction (must have status="unknown")
    - Routes to the correct processor mock
    - Normalizes the heterogeneous response
    - Returns recovered state + recommended action
    """
    try:
        result = await recover_transaction(transaction_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Processor error: {str(e)}")

    return RecoverResponse(
        transaction_id=result.transaction_id,
        original_status=result.original_status,
        recovered_state=result.recovered_state,
        processor_timestamp=result.processor_timestamp,
        recommended_action=result.recommended_action,
        next_retry_at=result.next_retry_at,
        processor_raw_response=result.processor_raw_response,
        recovered_at=result.recovered_at,
    )


@router.get("/{transaction_id}/duplicates", response_model=DuplicateReport)
def get_duplicates(transaction_id: str, db: Session = Depends(get_db)):
    """
    Find duplicate transactions for the given transaction.

    Detection criteria:
    - Same customer_id
    - Amount within ±5%
    - Timestamp within ±10 minutes

    Returns confidence scores and recommended actions per duplicate pair.
    """
    try:
        duplicates = find_duplicates(transaction_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return DuplicateReport(
        transaction_id=transaction_id,
        duplicates_found=len(duplicates),
        duplicates=duplicates,
    )
