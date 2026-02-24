"""
Unit tests for app/services/recovery.py.

Mocks all processor calls so tests are fast and deterministic.
Covers: happy paths, stale detection, idempotency, error propagation.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.services import recovery as recovery_module
from app.services.recovery import recover_transaction, STALE_THRESHOLD_DAYS
from app import models
from tests.conftest import make_txn

BASE_TIME = datetime(2024, 1, 15, 10, 0, 0)

# Canonical raw responses per processor
BANCOSUR_APPROVED = {
    "status": "APPROVED",
    "timestamp": "2024-01-15T10:23:45+00:00",
    "processor": "BancoSur",
    "authorization_code": "BS123456",
    "response_code": "00",
}
BANCOSUR_DECLINED = {
    "status": "DECLINED",
    "timestamp": "2024-01-15T10:23:45+00:00",
    "processor": "BancoSur",
    "authorization_code": None,
    "response_code": "05",
}
BANCOSUR_PENDING = {
    "status": "PENDING",
    "timestamp": "2024-01-15T10:23:45+00:00",
    "processor": "BancoSur",
    "authorization_code": None,
    "response_code": "00",
}
MEXPAY_SUCCESS = {
    "payment_status": "success",
    "processed_at": 1705312425,
    "gateway": "MexPay",
    "approved": True,
}
ANDESPSP_APROBADA = {
    "transaction_state": "aprobada",
    "fecha_hora": "15/01/2024 10:23:45",
    "procesador": "AndesPSP",
}
CASHVOUCHER_PAID = {
    "state": "PAID",
    "issued_at": "Mon, 15 Jan 2024 10:23:45 +0000",
    "issuer": "CashVoucher",
}


def mock_processor(raw_response):
    """Return an AsyncMock that simulates a processor returning raw_response."""
    m = AsyncMock()
    m.query_transaction = AsyncMock(return_value=raw_response)
    return m


# ---------------------------------------------------------------------------
# Happy paths — state recovery persisted to DB
# ---------------------------------------------------------------------------
class TestRecoverHappyPath:
    async def test_bancosur_approved(self, db):
        txn = make_txn(db, "txn_1", processor="bancosur", real_state="approved")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            result = await recover_transaction("txn_1", db)

        assert result.recovered_state == "approved"
        assert result.recommended_action == "fulfill_order"
        assert result.next_retry_at is None
        assert result.stale_transaction_warning is None

    async def test_bancosur_declined(self, db):
        txn = make_txn(db, "txn_1", processor="bancosur", real_state="declined")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_DECLINED)}):
            result = await recover_transaction("txn_1", db)

        assert result.recovered_state == "declined"
        assert result.recommended_action == "refund_customer"
        assert result.next_retry_at is None

    async def test_pending_sets_next_retry_at(self, db):
        txn = make_txn(db, "txn_1", processor="bancosur", real_state="pending")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_PENDING)}):
            result = await recover_transaction("txn_1", db)

        assert result.recovered_state == "pending"
        assert result.recommended_action == "wait_for_settlement"
        assert result.next_retry_at is not None

    async def test_mexpay_success(self, db):
        txn = make_txn(db, "txn_1", processor="mexpay", real_state="approved")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"mexpay": mock_processor(MEXPAY_SUCCESS)}):
            result = await recover_transaction("txn_1", db)

        assert result.recovered_state == "approved"

    async def test_andespsp_aprobada(self, db):
        txn = make_txn(db, "txn_1", processor="andespsp", real_state="approved")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"andespsp": mock_processor(ANDESPSP_APROBADA)}):
            result = await recover_transaction("txn_1", db)

        assert result.recovered_state == "approved"

    async def test_cashvoucher_paid(self, db):
        txn = make_txn(db, "txn_1", processor="cashvoucher", real_state="approved")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"cashvoucher": mock_processor(CASHVOUCHER_PAID)}):
            result = await recover_transaction("txn_1", db)

        assert result.recovered_state == "approved"

    async def test_recovery_persisted_to_db(self, db):
        """recovered_state and recovered_at must be written to the DB row."""
        txn = make_txn(db, "txn_1", processor="bancosur", real_state="approved")
        assert txn.recovered_state is None

        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            await recover_transaction("txn_1", db)

        db.refresh(txn)
        assert txn.recovered_state == "approved"
        assert txn.recovered_at is not None

    async def test_raw_response_included_in_result(self, db):
        make_txn(db, "txn_1", processor="bancosur", real_state="approved")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            result = await recover_transaction("txn_1", db)

        assert result.processor_raw_response == BANCOSUR_APPROVED


# ---------------------------------------------------------------------------
# Stale transaction handling
# ---------------------------------------------------------------------------
class TestStaleTransactions:
    async def test_stale_transaction_sets_warning(self, db):
        stale_time = datetime(2020, 1, 1, 0, 0, 0)  # well over 30 days ago
        make_txn(db, "txn_stale", processor="bancosur", created_at=stale_time)
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            result = await recover_transaction("txn_stale", db)

        assert result.stale_transaction_warning is not None
        assert "30" in result.stale_transaction_warning  # mentions threshold

    async def test_stale_transaction_forces_manual_review(self, db):
        """Even if processor says APPROVED, stale txn must escalate."""
        stale_time = datetime(2020, 1, 1, 0, 0, 0)
        make_txn(db, "txn_stale", processor="bancosur", created_at=stale_time,
                 real_state="approved")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            result = await recover_transaction("txn_stale", db)

        assert result.recommended_action == "escalate_to_manual_review"
        assert result.next_retry_at is None  # no retry for stale

    async def test_fresh_transaction_has_no_warning(self, db):
        # Explicitly pass a recent date — BASE_TIME is too old and would trigger stale logic
        recent = datetime.utcnow() - timedelta(hours=1)
        make_txn(db, "txn_fresh", processor="bancosur", created_at=recent)
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            result = await recover_transaction("txn_fresh", db)

        assert result.stale_transaction_warning is None

    async def test_exactly_30_days_old_is_not_stale(self, db):
        """Boundary: exactly 30 days old should NOT trigger the warning."""
        from datetime import timezone
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        make_txn(db, "txn_boundary", processor="bancosur", created_at=cutoff)
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            result = await recover_transaction("txn_boundary", db)

        assert result.stale_transaction_warning is None


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------
class TestRecoveryErrors:
    async def test_transaction_not_found_raises_value_error(self, db):
        with pytest.raises(ValueError, match="not found"):
            await recover_transaction("txn_nonexistent", db)

    async def test_unknown_processor_raises_value_error(self, db):
        make_txn(db, "txn_1", processor="unknown_bank")
        with pytest.raises(ValueError, match="Unknown processor"):
            await recover_transaction("txn_1", db)

    async def test_processor_runtime_error_propagates(self, db):
        make_txn(db, "txn_1", processor="bancosur")
        failing = AsyncMock()
        failing.query_transaction = AsyncMock(side_effect=RuntimeError("503 Service Unavailable"))
        with patch.dict(recovery_module.PROCESSOR_MAP, {"bancosur": failing}):
            with pytest.raises(RuntimeError, match="503"):
                await recover_transaction("txn_1", db)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
class TestIdempotency:
    async def test_already_recovered_returns_cached_without_calling_processor(self, db):
        """If status != 'unknown', the processor must NOT be called."""
        txn = make_txn(db, "txn_1", processor="bancosur", recovered_state="approved")
        # Manually flip status to non-unknown to simulate already-recovered
        txn.status = "processed"
        db.commit()

        spy = AsyncMock()
        spy.query_transaction = AsyncMock(return_value=BANCOSUR_APPROVED)
        with patch.dict(recovery_module.PROCESSOR_MAP, {"bancosur": spy}):
            result = await recover_transaction("txn_1", db)

        spy.query_transaction.assert_not_called()
        assert result.recovered_state == "approved"
