from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class RecoverResponse(BaseModel):
    transaction_id: str
    original_status: str
    recovered_state: str
    processor_timestamp: Optional[str]
    recommended_action: str
    processor_raw_response: Dict[str, Any]
    recovered_at: datetime

    class Config:
        orm_mode = True


class DuplicateEntry(BaseModel):
    duplicate_transaction_id: str
    confidence_score: int
    time_gap_seconds: float
    recommendation: str
    reasoning: str


class DuplicateReport(BaseModel):
    transaction_id: str
    duplicates_found: int
    duplicates: List[DuplicateEntry]


class BulkResultCounts(BaseModel):
    approved: int = 0
    declined: int = 0
    pending: int = 0
    still_unknown: int = 0
    errors: int = 0


class BulkSummary(BaseModel):
    total_processed: int
    results: BulkResultCounts
    duplicates_detected: int
    total_recommended_refund_amount: float
    refund_currency_breakdown: Dict[str, float]
    transactions: List[RecoverResponse]
    processing_time_ms: int


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
