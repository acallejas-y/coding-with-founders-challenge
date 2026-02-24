import asyncio
import random
from datetime import datetime, timezone
from typing import Dict, Any
from app.processors.base import BaseProcessor


STATE_MAP = {
    "approved": "APPROVED",
    "declined": "DECLINED",
    "pending": "PENDING",
    "unknown": "UNKNOWN",
}


class BancoSurProcessor(BaseProcessor):
    """
    BancoSur Gateway mock.
    Status field: `status`
    Status values: APPROVED / DECLINED / PENDING / UNKNOWN
    Timestamp format: ISO8601
    Error rate: ~5%
    """

    @property
    def processor_name(self) -> str:
        return "bancosur"

    async def query_transaction(self, transaction_id: str, real_state: str) -> Dict[str, Any]:
        # Simulate network latency 10-200ms
        await asyncio.sleep(random.uniform(0.01, 0.2))

        # ~5% chance of 503-style error
        if random.random() < 0.05:
            raise RuntimeError("BancoSur: 503 Service Unavailable")

        status = STATE_MAP.get(real_state, "UNKNOWN")
        timestamp = datetime.now(timezone.utc).isoformat()

        return {
            "transaction_id": transaction_id,
            "status": status,
            "timestamp": timestamp,
            "processor": "BancoSur",
            "authorization_code": f"BS{random.randint(100000, 999999)}" if status == "APPROVED" else None,
            "response_code": "00" if status == "APPROVED" else "05",
        }
