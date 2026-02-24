"""
Unit tests for app/services/duplicate.py.

Covers: detection criteria (customer/amount/window), confidence scoring,
duplicate_type classification, recommendation logic for all state pairs,
edge cases (null customer_id, not found).
"""
import pytest
from datetime import datetime, timedelta

from app.services.duplicate import find_duplicates, _confidence_score, _duplicate_type
from app import models
from tests.conftest import make_txn

BASE_TIME = datetime(2024, 1, 15, 10, 0, 0)


# ---------------------------------------------------------------------------
# Detection criteria
# ---------------------------------------------------------------------------
class TestDuplicateDetection:
    def test_detects_exact_amount_same_customer_within_window(self, db):
        t1 = make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        t2 = make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                      created_at=BASE_TIME + timedelta(seconds=30))
        results = find_duplicates("txn_a", db)
        assert len(results) == 1
        assert results[0].duplicate_transaction_id == "txn_b"

    def test_detects_amount_within_5_percent(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        # 1049.99 is within 5% of 1000 (threshold = 1050)
        make_txn(db, "txn_b", customer_id="cust_1", amount=1049.99,
                 created_at=BASE_TIME + timedelta(minutes=2))
        results = find_duplicates("txn_a", db)
        assert len(results) == 1

    def test_ignores_amount_outside_5_percent(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        make_txn(db, "txn_b", customer_id="cust_1", amount=1060.0,
                 created_at=BASE_TIME + timedelta(minutes=2))
        assert find_duplicates("txn_a", db) == []

    def test_ignores_transactions_outside_10_minute_window(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(minutes=11))
        assert find_duplicates("txn_a", db) == []

    def test_ignores_different_customer_same_amount(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        make_txn(db, "txn_b", customer_id="cust_2", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30))
        assert find_duplicates("txn_a", db) == []

    def test_null_customer_id_returns_empty(self, db):
        make_txn(db, "txn_a", customer_id=None, amount=1000.0, created_at=BASE_TIME)
        make_txn(db, "txn_b", customer_id=None, amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30))
        assert find_duplicates("txn_a", db) == []

    def test_transaction_not_found_raises_value_error(self, db):
        with pytest.raises(ValueError, match="not found"):
            find_duplicates("txn_nonexistent", db)

    def test_results_sorted_by_confidence_descending(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        # High confidence: exact amount, same processor, 10s gap
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=10), processor="bancosur")
        # Lower confidence: within 5%, different processor, 8min gap
        make_txn(db, "txn_c", customer_id="cust_1", amount=1030.0,
                 created_at=BASE_TIME + timedelta(minutes=8), processor="mexpay")
        results = find_duplicates("txn_a", db)
        assert len(results) == 2
        assert results[0].confidence_score >= results[1].confidence_score


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------
class TestConfidenceScoring:
    def _make_pair(self, db, amount_a, amount_b, processor_a, processor_b, gap_seconds):
        t1 = make_txn(db, "txn_a", customer_id="cust_1", amount=amount_a,
                      processor=processor_a, created_at=BASE_TIME)
        t2 = make_txn(db, "txn_b", customer_id="cust_1", amount=amount_b,
                      processor=processor_b,
                      created_at=BASE_TIME + timedelta(seconds=gap_seconds))
        return t1, t2

    def test_max_score_exact_amount_same_processor_30s_gap(self, db):
        t1, t2 = self._make_pair(db, 1000.0, 1000.0, "bancosur", "bancosur", 30)
        # exact(40) + same_processor(20) + gap<120s(30) = 90
        assert _confidence_score(t1, t2) == 90

    def test_near_amount_different_processor_large_gap(self, db):
        t1, t2 = self._make_pair(db, 1000.0, 1040.0, "bancosur", "mexpay", 500)
        # near(20) + diff_proc(0) + gap>=300s(10) = 30
        assert _confidence_score(t1, t2) == 30

    def test_exact_amount_same_processor_medium_gap(self, db):
        t1, t2 = self._make_pair(db, 1000.0, 1000.0, "bancosur", "bancosur", 200)
        # exact(40) + same_proc(20) + gap<300s(20) = 80
        assert _confidence_score(t1, t2) == 80

    def test_score_capped_at_100(self, db):
        t1, t2 = self._make_pair(db, 1000.0, 1000.0, "bancosur", "bancosur", 1)
        assert _confidence_score(t1, t2) <= 100


# ---------------------------------------------------------------------------
# Duplicate type classification
# ---------------------------------------------------------------------------
class TestDuplicateType:
    def test_accidental_retry_high_confidence_fast_same_processor(self):
        # score=90, gap=30s, same_processor → accidental_retry
        assert _duplicate_type(score=90, gap_seconds=30, same_processor=True) == "accidental_retry"

    def test_accidental_retry_requires_same_processor(self):
        # score=90, gap=30s but different processor → suspected_retry
        assert _duplicate_type(score=90, gap_seconds=30, same_processor=False) == "suspected_retry"

    def test_accidental_retry_requires_gap_under_120s(self):
        # score=90, gap=150s, same processor → suspected_retry (gap too large)
        assert _duplicate_type(score=90, gap_seconds=150, same_processor=True) == "suspected_retry"

    def test_suspected_retry_moderate_confidence_within_5_minutes(self):
        assert _duplicate_type(score=70, gap_seconds=200, same_processor=False) == "suspected_retry"

    def test_likely_legitimate_low_confidence(self):
        assert _duplicate_type(score=30, gap_seconds=500, same_processor=False) == "likely_legitimate"

    def test_duplicate_entry_includes_type_field(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0, created_at=BASE_TIME)
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30))
        results = find_duplicates("txn_a", db)
        assert len(results) == 1
        assert results[0].duplicate_type in ("accidental_retry", "suspected_retry", "likely_legitimate")


# ---------------------------------------------------------------------------
# Recommendation logic (all state-pair combinations)
# ---------------------------------------------------------------------------
class TestRecommendationLogic:
    def test_both_approved_recommends_refund_duplicate(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME, recovered_state="approved")
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30), recovered_state="approved")
        results = find_duplicates("txn_a", db)
        assert results[0].recommendation == "refund_duplicate"

    def test_approved_plus_unknown_recommends_mark_as_duplicate(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME, recovered_state="approved")
        # real_state="unknown" so the service sees it as unresolved (no recovered_state,
        # and real_state is not "approved") → target=approved, candidate=unknown
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30),
                 recovered_state=None, real_state="unknown")
        results = find_duplicates("txn_a", db)
        assert results[0].recommendation == "mark_as_duplicate"

    def test_both_declined_recommends_no_action(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME, recovered_state="declined")
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30), recovered_state="declined")
        results = find_duplicates("txn_a", db)
        assert results[0].recommendation == "no_action"

    def test_approved_plus_declined_recommends_no_action(self, db):
        make_txn(db, "txn_a", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME, recovered_state="approved")
        make_txn(db, "txn_b", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30), recovered_state="declined")
        results = find_duplicates("txn_a", db)
        assert results[0].recommendation == "no_action"

    def test_refund_duplicate_keeps_earlier_transaction(self, db):
        """When both are approved, the EARLIER transaction should be kept."""
        make_txn(db, "txn_early", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME, recovered_state="approved")
        make_txn(db, "txn_late", customer_id="cust_1", amount=1000.0,
                 created_at=BASE_TIME + timedelta(seconds=30), recovered_state="approved")
        results = find_duplicates("txn_early", db)
        assert results[0].recommendation == "refund_duplicate"
        assert "txn_early" in results[0].reasoning  # kept
        assert "txn_late" in results[0].reasoning   # refunded
