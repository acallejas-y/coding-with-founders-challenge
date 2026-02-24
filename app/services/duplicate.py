"""
Duplicate detection service.

Detection criteria:
- Same customer_id (non-null)
- Amount within ±5%
- Timestamp within ±10 minutes
- Different transaction ID

Confidence scoring (0-100):
  Amount match:  +40 (exact) or +20 (within 5%)
  Same processor: +20
  Time gap:       +30 (<2min), +20 (<5min), +10 (otherwise)

Recommendation logic:
  Both approved          → keep first (earlier), refund_duplicate for later
  One approved + unknown → first is ground truth, mark unknown as duplicate
  Both declined          → no_action (no double charge)
  One approved + declined → no_action (different outcomes, no duplicate charge)
  Other                  → manual_review
"""
from datetime import timedelta
from typing import List
from sqlalchemy.orm import Session

from app import models
from app.schemas.responses import DuplicateEntry


def _confidence_score(
    target: models.Transaction,
    candidate: models.Transaction,
) -> int:
    score = 0

    # Amount component (max 40)
    if abs(target.amount - candidate.amount) < 0.01:
        score += 40  # exact match
    else:
        score += 20  # within 5% (already filtered by query)

    # Same processor component (max 20)
    if target.processor == candidate.processor:
        score += 20

    # Time gap component (max 30)
    gap_seconds = abs(
        (target.created_at - candidate.created_at).total_seconds()
    )
    if gap_seconds < 120:
        score += 30
    elif gap_seconds < 300:
        score += 20
    else:
        score += 10

    return min(score, 100)


def _recommendation(
    target: models.Transaction,
    candidate: models.Transaction,
    target_state: str,
    candidate_state: str,
) -> tuple:
    """Returns (recommendation, reasoning) tuple."""
    # Determine effective states
    t_state = target_state
    c_state = candidate_state

    if t_state == "approved" and c_state == "approved":
        # Both approved — keep earlier, refund later
        if target.created_at <= candidate.created_at:
            return (
                "refund_duplicate",
                f"Both approved. Keep {target.id} (earlier). Refund {candidate.id}.",
            )
        else:
            return (
                "refund_duplicate",
                f"Both approved. Keep {candidate.id} (earlier). Refund {target.id}.",
            )

    if t_state == "approved" and c_state == "unknown":
        return (
            "mark_as_duplicate",
            f"{target.id} approved. {candidate.id} is an unresolved duplicate.",
        )

    if t_state == "unknown" and c_state == "approved":
        return (
            "mark_as_duplicate",
            f"{candidate.id} approved. {target.id} is an unresolved duplicate.",
        )

    if t_state == "declined" and c_state == "declined":
        return (
            "no_action",
            "Both transactions declined. No duplicate charge occurred.",
        )

    if (t_state == "approved" and c_state == "declined") or \
       (t_state == "declined" and c_state == "approved"):
        return (
            "no_action",
            "Transactions have different outcomes. Not a duplicate charge.",
        )

    return (
        "manual_review",
        f"States: {t_state}/{c_state}. Manual review recommended.",
    )


def find_duplicates(transaction_id: str, db: Session) -> List[DuplicateEntry]:
    """
    Find duplicate transactions for the given transaction_id.

    Returns list of DuplicateEntry objects sorted by confidence score descending.
    """
    target = db.query(models.Transaction).filter(
        models.Transaction.id == transaction_id
    ).first()

    if target is None:
        raise ValueError(f"Transaction {transaction_id} not found")

    if target.customer_id is None:
        # Cannot detect duplicates without customer_id
        return []

    window_start = target.created_at - timedelta(minutes=10)
    window_end = target.created_at + timedelta(minutes=10)
    amount_low = target.amount * 0.95
    amount_high = target.amount * 1.05

    candidates = db.query(models.Transaction).filter(
        models.Transaction.customer_id == target.customer_id,
        models.Transaction.amount.between(amount_low, amount_high),
        models.Transaction.created_at.between(window_start, window_end),
        models.Transaction.id != target.id,
    ).all()

    results = []
    for candidate in candidates:
        score = _confidence_score(target, candidate)
        gap_seconds = abs(
            (target.created_at - candidate.created_at).total_seconds()
        )

        # Use recovered_state if available, else real_state as proxy for detection
        target_state = target.recovered_state or target.real_state
        candidate_state = candidate.recovered_state or candidate.real_state

        recommendation, reasoning = _recommendation(
            target, candidate, target_state, candidate_state
        )

        results.append(DuplicateEntry(
            duplicate_transaction_id=candidate.id,
            confidence_score=score,
            time_gap_seconds=gap_seconds,
            recommendation=recommendation,
            reasoning=reasoning,
        ))

    # Sort by confidence score descending
    results.sort(key=lambda x: x.confidence_score, reverse=True)
    return results
