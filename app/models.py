from sqlalchemy import Column, String, Float, DateTime
from app.database import Base
import uuid


def generate_id():
    return f"txn_{uuid.uuid4().hex[:8]}"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=generate_id)
    customer_id = Column(String, nullable=True, index=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False)
    processor = Column(String, nullable=False)
    status = Column(String, nullable=False, default="unknown")
    real_state = Column(String, nullable=False)  # ground truth for mocks
    created_at = Column(DateTime, nullable=False)
    recovered_state = Column(String, nullable=True)
    recovered_at = Column(DateTime, nullable=True)
    processor_timestamp = Column(String, nullable=True)
    notes = Column(String, nullable=True)
