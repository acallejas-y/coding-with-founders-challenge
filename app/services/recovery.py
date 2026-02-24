"""
Transaction recovery service.

Orchestrates:
1. Fetch transaction from DB
2. Route to correct processor mock
3. Call processor (with error handling)
4. Normalize response
5. Apply business logic → recommendation
6. Persist recovered_state + recovered_at
"""
from datetime import datetime, timezone
from typing import Dict, Any
from sqlalchemy.orm import Session

from app import models
from app.processors.bancosur import BancoSurProcessor
from app.processors.mexpay import MexPayProcessor
from app.processors.andespsp import AndesPSPProcessor
from app.processors.cashvoucher import CashVoucherProcessor
from app.services.normalizer import normalize, get_recommended_action, get_next_retry_at


PROCESSOR_MAP = {
    "bancosur": BancoSurProcessor(),
    "mexpay": MexPayProcessor(),
    "andespsp": AndesPSPProcessor(),
    "cashvoucher": CashVoucherProcessor(),
}


class RecoveryResult:
    def __init__(
        self,
        transaction_id: str,
        original_status: str,
        recovered_state: str,
        processor_timestamp,
        recommended_action: str,
        processor_raw_response: Dict[str, Any],
        recovered_at: datetime,
        next_retry_at=None,
    ):
        self.transaction_id = transaction_id
        self.original_status = original_status
        self.recovered_state = recovered_state
        self.processor_timestamp = processor_timestamp
        self.recommended_action = recommended_action
        self.processor_raw_response = processor_raw_response
        self.recovered_at = recovered_at
        self.next_retry_at = next_retry_at


async def recover_transaction(transaction_id: str, db: Session) -> RecoveryResult:
    """
    Recover a single transaction.

    Raises:
        ValueError: if transaction not found or not in unknown state
        RuntimeError: if processor returns an error
    """
    txn = db.query(models.Transaction).filter(
        models.Transaction.id == transaction_id
    ).first()

    if txn is None:
        raise ValueError(f"Transaction {transaction_id} not found")

    if txn.status != "unknown":
        # Already recovered — return cached result
        return RecoveryResult(
            transaction_id=txn.id,
            original_status=txn.status,
            recovered_state=txn.recovered_state or txn.status,
            processor_timestamp=txn.processor_timestamp,
            recommended_action=get_recommended_action(txn.recovered_state or txn.status),
            processor_raw_response={"cached": True, "status": txn.recovered_state},
            recovered_at=txn.recovered_at or txn.created_at,
        )

    processor = PROCESSOR_MAP.get(txn.processor)
    if processor is None:
        raise ValueError(f"Unknown processor: {txn.processor}")

    # Query processor (may raise RuntimeError on 503)
    raw_response = await processor.query_transaction(txn.id, txn.real_state)

    # Normalize to standard schema
    normalized = normalize(txn.processor, raw_response)

    recovered_at = datetime.now(timezone.utc)
    recommended_action = get_recommended_action(normalized.normalized_state)
    next_retry_at = get_next_retry_at(txn.processor, normalized.normalized_state)

    # Persist recovery result
    txn.recovered_state = normalized.normalized_state
    txn.recovered_at = recovered_at
    txn.processor_timestamp = normalized.processor_timestamp
    # Keep status as "unknown" to preserve original state for audit trail
    db.commit()
    db.refresh(txn)

    return RecoveryResult(
        transaction_id=txn.id,
        original_status="unknown",
        recovered_state=normalized.normalized_state,
        processor_timestamp=normalized.processor_timestamp,
        recommended_action=recommended_action,
        processor_raw_response=raw_response,
        recovered_at=recovered_at,
        next_retry_at=next_retry_at,
    )
