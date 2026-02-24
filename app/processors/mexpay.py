import asyncio
import random
import time
from typing import Dict, Any
from app.processors.base import BaseProcessor


STATE_MAP = {
    "approved": "success",
    "declined": "failed",
    "pending": "processing",
    "unknown": "indeterminate",
}


class MexPayProcessor(BaseProcessor):
    """
    MexPay mock.
    Status field: `payment_status`
    Status values: success / failed / processing / indeterminate
    Timestamp format: Unix epoch (integer)
    Error rate: ~5%
    """

    @property
    def processor_name(self) -> str:
        return "mexpay"

    async def query_transaction(self, transaction_id: str, real_state: str) -> Dict[str, Any]:
        await asyncio.sleep(random.uniform(0.01, 0.2))

        if random.random() < 0.05:
            raise RuntimeError("MexPay: connection timeout")

        payment_status = STATE_MAP.get(real_state, "indeterminate")
        epoch_timestamp = int(time.time())

        return {
            "id": transaction_id,
            "payment_status": payment_status,
            "processed_at": epoch_timestamp,
            "gateway": "MexPay",
            "mx_code": random.randint(1000, 9999),
            "approved": payment_status == "success",
        }
