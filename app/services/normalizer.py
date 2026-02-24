"""
Normalizes heterogeneous processor responses to a standard schema.

Each processor uses different field names, status vocabularies, and timestamp formats.
This module maps all of them to a canonical normalized form.
"""
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import time


# Standard status vocabulary
NORMALIZED_STATES = {
    # BancoSur
    "APPROVED": "approved",
    "DECLINED": "declined",
    "PENDING": "pending",
    "UNKNOWN": "unknown",
    # MexPay
    "success": "approved",
    "failed": "declined",
    "processing": "pending",
    "indeterminate": "unknown",
    # AndesPSP
    "aprobada": "approved",
    "rechazada": "declined",
    "pendiente": "pending",
    "desconocido": "unknown",
    # CashVoucher
    "PAID": "approved",
    "REJECTED": "declined",
    "WAITING": "pending",
    "ERROR": "unknown",
}


def _parse_timestamp_bancosur(raw: Dict[str, Any]) -> Optional[str]:
    """ISO8601 → return as-is."""
    return raw.get("timestamp")


def _parse_timestamp_mexpay(raw: Dict[str, Any]) -> Optional[str]:
    """Unix epoch integer → ISO8601."""
    epoch = raw.get("processed_at")
    if epoch is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, OSError):
        return None


def _parse_timestamp_andespsp(raw: Dict[str, Any]) -> Optional[str]:
    """DD/MM/YYYY HH:MM:SS → ISO8601."""
    ts = raw.get("fecha_hora")
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%d/%m/%Y %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _parse_timestamp_cashvoucher(raw: Dict[str, Any]) -> Optional[str]:
    """RFC2822 → ISO8601."""
    from email.utils import parsedate_to_datetime
    ts = raw.get("issued_at")
    if not ts:
        return None
    try:
        dt = parsedate_to_datetime(ts)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


TIMESTAMP_PARSERS = {
    "bancosur": _parse_timestamp_bancosur,
    "mexpay": _parse_timestamp_mexpay,
    "andespsp": _parse_timestamp_andespsp,
    "cashvoucher": _parse_timestamp_cashvoucher,
}

STATUS_FIELD_MAP = {
    "bancosur": "status",
    "mexpay": "payment_status",
    "andespsp": "transaction_state",
    "cashvoucher": "state",
}


class NormalizedResponse:
    def __init__(
        self,
        normalized_state: str,
        processor_timestamp: Optional[str],
        raw_response: Dict[str, Any],
    ):
        self.normalized_state = normalized_state
        self.processor_timestamp = processor_timestamp
        self.raw_response = raw_response


def normalize(processor_name: str, raw_response: Dict[str, Any]) -> NormalizedResponse:
    """
    Maps a processor's raw response to a standard NormalizedResponse.

    Args:
        processor_name: One of bancosur, mexpay, andespsp, cashvoucher
        raw_response: The dict returned by the processor mock

    Returns:
        NormalizedResponse with canonical state and ISO8601 timestamp
    """
    status_field = STATUS_FIELD_MAP.get(processor_name)
    if not status_field:
        raise ValueError(f"Unknown processor: {processor_name}")

    raw_status = raw_response.get(status_field, "")
    normalized_state = NORMALIZED_STATES.get(raw_status, "unknown")

    timestamp_parser = TIMESTAMP_PARSERS.get(processor_name)
    processor_timestamp = timestamp_parser(raw_response) if timestamp_parser else None

    return NormalizedResponse(
        normalized_state=normalized_state,
        processor_timestamp=processor_timestamp,
        raw_response=raw_response,
    )


RECOMMENDED_ACTIONS = {
    "approved": "fulfill_order",
    "declined": "refund_customer",
    "pending": "wait_for_settlement",
    "unknown": "escalate_to_manual_review",
}


def get_recommended_action(normalized_state: str) -> str:
    return RECOMMENDED_ACTIONS.get(normalized_state, "escalate_to_manual_review")


# Stretch Goal A: retry schedule per processor type (for pending/unknown states)
RETRY_DELAY_SECONDS = {
    "bancosur": 5 * 60,       # +5 minutes
    "mexpay": 60 * 60,         # +1 hour
    "andespsp": 24 * 60 * 60,  # +24 hours
    "cashvoucher": 24 * 60 * 60,  # +24 hours
}


def get_next_retry_at(processor_name: str, normalized_state: str):
    """
    Returns next retry datetime for pending or unknown transactions.
    Returns None for resolved states (approved/declined).
    """
    if normalized_state not in ("pending", "unknown"):
        return None
    delay = RETRY_DELAY_SECONDS.get(processor_name, 60 * 60)
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone.utc) + timedelta(seconds=delay)
