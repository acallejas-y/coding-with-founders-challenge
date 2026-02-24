"""
Integration tests for all three API endpoints.

Uses FastAPI TestClient with the real app (minus lifespan).
The get_db dependency is overridden to use an in-memory SQLite session.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.services import recovery as recovery_module
from tests.conftest import make_txn

BASE_TIME = datetime(2024, 1, 15, 10, 0, 0)

BANCOSUR_APPROVED = {
    "status": "APPROVED",
    "timestamp": "2024-01-15T10:23:45+00:00",
    "processor": "BancoSur",
    "authorization_code": "BS999",
    "response_code": "00",
}
BANCOSUR_PENDING = {
    "status": "PENDING",
    "timestamp": "2024-01-15T10:23:45+00:00",
    "processor": "BancoSur",
    "authorization_code": None,
    "response_code": "00",
}


def mock_processor(raw):
    m = AsyncMock()
    m.query_transaction = AsyncMock(return_value=raw)
    return m


# ---------------------------------------------------------------------------
# POST /api/v1/transactions/{id}/recover
# ---------------------------------------------------------------------------
class TestRecoverEndpoint:
    def test_recover_returns_200_with_correct_body(self, client, db):
        make_txn(db, "txn_1", processor="bancosur", real_state="approved")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            resp = client.post("/api/v1/transactions/txn_1/recover")

        assert resp.status_code == 200
        body = resp.json()
        assert body["transaction_id"] == "txn_1"
        assert body["recovered_state"] == "approved"
        assert body["recommended_action"] == "fulfill_order"
        assert "processor_raw_response" in body
        assert "recovered_at" in body

    def test_recover_returns_404_for_unknown_id(self, client, db):
        resp = client.post("/api/v1/transactions/txn_nonexistent/recover")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_recover_returns_502_on_processor_error(self, client, db):
        make_txn(db, "txn_1", processor="bancosur")
        failing = AsyncMock()
        failing.query_transaction = AsyncMock(side_effect=RuntimeError("503 Unavailable"))
        with patch.dict(recovery_module.PROCESSOR_MAP, {"bancosur": failing}):
            resp = client.post("/api/v1/transactions/txn_1/recover")

        assert resp.status_code == 502
        assert "503" in resp.json()["detail"]

    def test_recover_pending_includes_next_retry_at(self, client, db):
        make_txn(db, "txn_1", processor="bancosur", real_state="pending")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_PENDING)}):
            resp = client.post("/api/v1/transactions/txn_1/recover")

        assert resp.status_code == 200
        assert resp.json()["next_retry_at"] is not None

    def test_recover_stale_includes_warning(self, client, db):
        stale_time = datetime(2020, 1, 1)
        make_txn(db, "txn_stale", processor="bancosur", created_at=stale_time)
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            resp = client.post("/api/v1/transactions/txn_stale/recover")

        assert resp.status_code == 200
        body = resp.json()
        assert body["stale_transaction_warning"] is not None
        assert body["recommended_action"] == "escalate_to_manual_review"


# ---------------------------------------------------------------------------
# GET /api/v1/transactions/{id}/duplicates
# ---------------------------------------------------------------------------
class TestDuplicatesEndpoint:
    def test_returns_200_with_duplicates_found(self, client, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30))

        resp = client.get("/api/v1/transactions/txn_a/duplicates")
        assert resp.status_code == 200
        body = resp.json()
        assert body["transaction_id"] == "txn_a"
        assert body["duplicates_found"] == 1
        assert body["duplicates"][0]["duplicate_transaction_id"] == "txn_b"
        assert "confidence_score" in body["duplicates"][0]
        assert "duplicate_type" in body["duplicates"][0]
        assert "recommendation" in body["duplicates"][0]

    def test_returns_empty_list_when_no_duplicates(self, client, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0)
        resp = client.get("/api/v1/transactions/txn_a/duplicates")
        assert resp.status_code == 200
        assert resp.json()["duplicates_found"] == 0
        assert resp.json()["duplicates"] == []

    def test_returns_empty_list_for_null_customer_id(self, client, db):
        make_txn(db, "txn_a", customer_id=None, amount=1000.0)
        resp = client.get("/api/v1/transactions/txn_a/duplicates")
        assert resp.status_code == 200
        assert resp.json()["duplicates_found"] == 0

    def test_returns_404_for_unknown_transaction(self, client, db):
        resp = client.get("/api/v1/transactions/txn_nonexistent/duplicates")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/transactions/bulk-recover
# ---------------------------------------------------------------------------
class TestBulkRecoverEndpoint:
    def test_bulk_recover_returns_200_with_summary(self, client, db):
        for i in range(5):
            make_txn(db, f"txn_{i}", processor="bancosur", real_state="approved")

        ids = [f"txn_{i}" for i in range(5)]
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            resp = client.post("/api/v1/transactions/bulk-recover",
                               json={"transaction_ids": ids})

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_processed"] == 5
        assert body["results"]["approved"] == 5
        assert body["results"]["errors"] == 0
        assert "processing_time_ms" in body

    def test_bulk_recover_isolates_partial_failures(self, client, db):
        # 3 succeed, 1 unknown processor
        for i in range(3):
            make_txn(db, f"txn_{i}", processor="bancosur", real_state="approved")
        make_txn(db, "txn_bad", processor="unknown_bank")

        ids = [f"txn_{i}" for i in range(3)] + ["txn_bad"]
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            resp = client.post("/api/v1/transactions/bulk-recover",
                               json={"transaction_ids": ids})

        assert resp.status_code == 200
        body = resp.json()
        assert body["results"]["approved"] == 3
        assert body["results"]["errors"] == 1
        assert len(body["failed_transactions"]) == 1
        assert body["failed_transactions"][0]["transaction_id"] == "txn_bad"

    def test_bulk_recover_failed_transactions_include_error_message(self, client, db):
        make_txn(db, "txn_1", processor="bancosur")
        failing = AsyncMock()
        failing.query_transaction = AsyncMock(side_effect=RuntimeError("503 BancoSur down"))
        with patch.dict(recovery_module.PROCESSOR_MAP, {"bancosur": failing}):
            resp = client.post("/api/v1/transactions/bulk-recover",
                               json={"transaction_ids": ["txn_1"]})

        body = resp.json()
        assert body["results"]["errors"] == 1
        assert len(body["failed_transactions"]) == 1
        failed = body["failed_transactions"][0]
        assert failed["transaction_id"] == "txn_1"
        assert "503" in failed["error"]

    def test_bulk_recover_returns_422_for_empty_list(self, client, db):
        resp = client.post("/api/v1/transactions/bulk-recover",
                           json={"transaction_ids": []})
        assert resp.status_code == 422

    def test_bulk_recover_returns_422_for_over_500_ids(self, client, db):
        ids = [f"txn_{i}" for i in range(501)]
        resp = client.post("/api/v1/transactions/bulk-recover",
                           json={"transaction_ids": ids})
        assert resp.status_code == 422

    def test_bulk_recover_counts_pending_and_unknown(self, client, db):
        make_txn(db, "txn_pending", processor="bancosur", real_state="pending")
        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_PENDING)}):
            resp = client.post("/api/v1/transactions/bulk-recover",
                               json={"transaction_ids": ["txn_pending"]})

        body = resp.json()
        assert body["results"]["pending"] == 1

    def test_bulk_recover_detects_duplicates_and_tallies_refund(self, client, db):
        """Duplicate cluster: two approved txns → exactly 1 pair detected, refund tallied once."""
        make_txn(db, "txn_a", customer_id="cust_dup", amount=500.0,
                 created_at=BASE_TIME, processor="bancosur",
                 recovered_state="approved")
        make_txn(db, "txn_b", customer_id="cust_dup", amount=500.0,
                 created_at=BASE_TIME + timedelta(seconds=20), processor="bancosur",
                 recovered_state="approved")

        with patch.dict(recovery_module.PROCESSOR_MAP,
                        {"bancosur": mock_processor(BANCOSUR_APPROVED)}):
            resp = client.post("/api/v1/transactions/bulk-recover",
                               json={"transaction_ids": ["txn_a", "txn_b"]})

        body = resp.json()
        # Each pair counted once (not twice — one per side)
        assert body["duplicates_detected"] == 1
        assert body["total_recommended_refund_amount"] == 500.0


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
class TestHealthEndpoint:
    def test_health_returns_200(self, client, db):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
