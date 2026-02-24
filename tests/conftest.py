"""
Shared pytest fixtures for all test modules.

Uses an in-memory SQLite database (StaticPool) so every test
function gets a clean, isolated database — no disk I/O, no state leakage.
"""
import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from typing import Optional

from app.database import Base, get_db
from app import models


# ---------------------------------------------------------------------------
# In-memory database engine shared across all fixtures in a test session.
# StaticPool forces all SQLAlchemy connections to reuse the same underlying
# sqlite3 connection, which is required for in-memory SQLite.
# ---------------------------------------------------------------------------
TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)


@pytest.fixture(autouse=True)
def reset_db():
    """Drop and recreate all tables before each test for full isolation."""
    Base.metadata.drop_all(bind=TEST_ENGINE)
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def db(reset_db):
    """Yield a SQLAlchemy session backed by the in-memory test database."""
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db):
    """
    FastAPI TestClient with the real DB dependency overridden to use
    the in-memory test session.  The TestClient is NOT used as a context
    manager so the lifespan hook (which seeds the on-disk DB) is skipped.
    """
    from app.main import app

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper — not a fixture — so any test file can import and call it directly.
# ---------------------------------------------------------------------------
def make_txn(
    db,
    txn_id: str,
    customer_id: Optional[str] = "cust_001",
    amount: float = 1000.00,
    currency: str = "MXN",
    processor: str = "bancosur",
    real_state: str = "approved",
    created_at: Optional[datetime] = None,   # defaults to 1 hour ago (always fresh)
    recovered_state: Optional[str] = None,
    notes: Optional[str] = None,
) -> models.Transaction:
    if created_at is None:
        from datetime import timedelta
        created_at = datetime.utcnow() - timedelta(hours=1)
    txn = models.Transaction(
        id=txn_id,
        customer_id=customer_id,
        amount=amount,
        currency=currency,
        processor=processor,
        status="unknown",
        real_state=real_state,
        created_at=created_at,
        recovered_state=recovered_state,
        notes=notes,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn
