"""
Pure unit tests for app/services/normalizer.py.

No database required — all functions are pure transformations.
Covers: all 4 processors, timestamp format conversions, action vocabulary,
retry schedule, edge cases (missing fields, unknown processor).
"""
import time
import pytest
from datetime import datetime, timezone, timedelta
from email.utils import formatdate

from app.services.normalizer import (
    normalize,
    get_recommended_action,
    get_next_retry_at,
    RETRY_DELAY_SECONDS,
)


# ---------------------------------------------------------------------------
# normalize() — BancoSur (ISO8601 timestamps)
# ---------------------------------------------------------------------------
class TestNormalizeBancoSur:
    def test_approved(self):
        raw = {"status": "APPROVED", "timestamp": "2024-01-15T10:23:45+00:00"}
        r = normalize("bancosur", raw)
        assert r.normalized_state == "approved"

    def test_declined(self):
        raw = {"status": "DECLINED", "timestamp": "2024-01-15T10:23:45+00:00"}
        assert normalize("bancosur", raw).normalized_state == "declined"

    def test_pending(self):
        raw = {"status": "PENDING", "timestamp": "2024-01-15T10:23:45+00:00"}
        assert normalize("bancosur", raw).normalized_state == "pending"

    def test_unknown(self):
        raw = {"status": "UNKNOWN", "timestamp": "2024-01-15T10:23:45+00:00"}
        assert normalize("bancosur", raw).normalized_state == "unknown"

    def test_iso8601_timestamp_preserved(self):
        ts = "2024-01-15T10:23:45+00:00"
        raw = {"status": "APPROVED", "timestamp": ts}
        assert normalize("bancosur", raw).processor_timestamp == ts

    def test_raw_response_preserved(self):
        raw = {"status": "APPROVED", "timestamp": "2024-01-15T10:23:45+00:00", "extra": "data"}
        assert normalize("bancosur", raw).raw_response == raw


# ---------------------------------------------------------------------------
# normalize() — MexPay (Unix epoch timestamps)
# ---------------------------------------------------------------------------
class TestNormalizeMexPay:
    def test_success(self):
        raw = {"payment_status": "success", "processed_at": int(time.time())}
        assert normalize("mexpay", raw).normalized_state == "approved"

    def test_failed(self):
        raw = {"payment_status": "failed", "processed_at": 1705312425}
        assert normalize("mexpay", raw).normalized_state == "declined"

    def test_processing(self):
        raw = {"payment_status": "processing", "processed_at": 1705312425}
        assert normalize("mexpay", raw).normalized_state == "pending"

    def test_indeterminate(self):
        raw = {"payment_status": "indeterminate", "processed_at": 1705312425}
        assert normalize("mexpay", raw).normalized_state == "unknown"

    def test_epoch_converted_to_iso8601(self):
        epoch = 1705312425  # 2024-01-15T10:53:45Z
        raw = {"payment_status": "success", "processed_at": epoch}
        ts = normalize("mexpay", raw).processor_timestamp
        assert ts is not None
        # Must be parseable as ISO8601
        dt = datetime.fromisoformat(ts)
        assert dt.year == 2024

    def test_missing_epoch_returns_none_timestamp(self):
        raw = {"payment_status": "success"}
        assert normalize("mexpay", raw).processor_timestamp is None


# ---------------------------------------------------------------------------
# normalize() — AndesPSP (DD/MM/YYYY HH:MM:SS timestamps)
# ---------------------------------------------------------------------------
class TestNormalizeAndesPSP:
    def test_aprobada(self):
        raw = {"transaction_state": "aprobada", "fecha_hora": "15/01/2024 10:23:45"}
        assert normalize("andespsp", raw).normalized_state == "approved"

    def test_rechazada(self):
        raw = {"transaction_state": "rechazada", "fecha_hora": "15/01/2024 10:23:45"}
        assert normalize("andespsp", raw).normalized_state == "declined"

    def test_pendiente(self):
        raw = {"transaction_state": "pendiente", "fecha_hora": "15/01/2024 10:23:45"}
        assert normalize("andespsp", raw).normalized_state == "pending"

    def test_desconocido(self):
        raw = {"transaction_state": "desconocido", "fecha_hora": "15/01/2024 10:23:45"}
        assert normalize("andespsp", raw).normalized_state == "unknown"

    def test_ddmmyyyy_converted_to_iso8601(self):
        raw = {"transaction_state": "aprobada", "fecha_hora": "15/01/2024 10:23:45"}
        ts = normalize("andespsp", raw).processor_timestamp
        assert ts is not None
        assert "2024-01-15" in ts

    def test_missing_fecha_hora_returns_none(self):
        raw = {"transaction_state": "aprobada"}
        assert normalize("andespsp", raw).processor_timestamp is None

    def test_invalid_date_format_returns_none(self):
        raw = {"transaction_state": "aprobada", "fecha_hora": "not-a-date"}
        assert normalize("andespsp", raw).processor_timestamp is None


# ---------------------------------------------------------------------------
# normalize() — CashVoucher (RFC2822 timestamps)
# ---------------------------------------------------------------------------
class TestNormalizeCashVoucher:
    def test_paid(self):
        raw = {"state": "PAID", "issued_at": formatdate(localtime=True)}
        assert normalize("cashvoucher", raw).normalized_state == "approved"

    def test_rejected(self):
        raw = {"state": "REJECTED", "issued_at": formatdate(localtime=True)}
        assert normalize("cashvoucher", raw).normalized_state == "declined"

    def test_waiting(self):
        raw = {"state": "WAITING", "issued_at": formatdate(localtime=True)}
        assert normalize("cashvoucher", raw).normalized_state == "pending"

    def test_error(self):
        raw = {"state": "ERROR", "issued_at": formatdate(localtime=True)}
        assert normalize("cashvoucher", raw).normalized_state == "unknown"

    def test_rfc2822_converted_to_iso8601(self):
        raw = {"state": "PAID", "issued_at": "Mon, 15 Jan 2024 10:23:45 +0000"}
        ts = normalize("cashvoucher", raw).processor_timestamp
        assert ts is not None
        assert "2024-01-15" in ts

    def test_missing_issued_at_returns_none(self):
        raw = {"state": "PAID"}
        assert normalize("cashvoucher", raw).processor_timestamp is None


# ---------------------------------------------------------------------------
# normalize() — edge cases
# ---------------------------------------------------------------------------
class TestNormalizeEdgeCases:
    def test_unknown_processor_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown processor"):
            normalize("unknown_bank", {"status": "OK"})

    def test_missing_status_field_defaults_to_unknown(self):
        # BancoSur response with no "status" key
        raw = {"timestamp": "2024-01-15T10:23:45+00:00"}
        assert normalize("bancosur", raw).normalized_state == "unknown"

    def test_unrecognized_status_value_defaults_to_unknown(self):
        raw = {"status": "PROCESSING", "timestamp": "2024-01-15T10:23:45+00:00"}
        assert normalize("bancosur", raw).normalized_state == "unknown"


# ---------------------------------------------------------------------------
# get_recommended_action()
# ---------------------------------------------------------------------------
class TestRecommendedActions:
    def test_approved_fulfills_order(self):
        assert get_recommended_action("approved") == "fulfill_order"

    def test_declined_refunds_customer(self):
        assert get_recommended_action("declined") == "refund_customer"

    def test_pending_waits_for_settlement(self):
        assert get_recommended_action("pending") == "wait_for_settlement"

    def test_unknown_escalates_to_manual_review(self):
        assert get_recommended_action("unknown") == "escalate_to_manual_review"

    def test_unrecognized_state_escalates(self):
        assert get_recommended_action("bogus_state") == "escalate_to_manual_review"


# ---------------------------------------------------------------------------
# get_next_retry_at()
# ---------------------------------------------------------------------------
class TestRetrySchedule:
    def _assert_delay(self, result, expected_seconds: int, tolerance: int = 2):
        assert result is not None
        expected = datetime.now(timezone.utc) + timedelta(seconds=expected_seconds)
        assert abs((result - expected).total_seconds()) < tolerance

    def test_bancosur_pending_retries_in_5_minutes(self):
        self._assert_delay(
            get_next_retry_at("bancosur", "pending"),
            RETRY_DELAY_SECONDS["bancosur"],
        )

    def test_mexpay_unknown_retries_in_1_hour(self):
        self._assert_delay(
            get_next_retry_at("mexpay", "unknown"),
            RETRY_DELAY_SECONDS["mexpay"],
        )

    def test_andespsp_pending_retries_in_24_hours(self):
        self._assert_delay(
            get_next_retry_at("andespsp", "pending"),
            RETRY_DELAY_SECONDS["andespsp"],
        )

    def test_cashvoucher_unknown_retries_in_24_hours(self):
        self._assert_delay(
            get_next_retry_at("cashvoucher", "unknown"),
            RETRY_DELAY_SECONDS["cashvoucher"],
        )

    def test_approved_returns_none(self):
        assert get_next_retry_at("bancosur", "approved") is None

    def test_declined_returns_none(self):
        assert get_next_retry_at("mexpay", "declined") is None

    def test_bancosur_delay_is_shorter_than_mexpay(self):
        assert RETRY_DELAY_SECONDS["bancosur"] < RETRY_DELAY_SECONDS["mexpay"]
