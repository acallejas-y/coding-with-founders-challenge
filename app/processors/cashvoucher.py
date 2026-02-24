import asyncio
import random
from email.utils import formatdate
from typing import Dict, Any
from app.processors.base import BaseProcessor


STATE_MAP = {
    "approved": "PAID",
    "declined": "REJECTED",
    "pending": "WAITING",
    "unknown": "ERROR",
}


class CashVoucherProcessor(BaseProcessor):
    """
    CashVoucher mock.
    Status field: `state`
    Status values: PAID / REJECTED / WAITING / ERROR
    Timestamp format: RFC2822 (email date format)
    Error rate: ~5%
    """

    @property
    def processor_name(self) -> str:
        return "cashvoucher"

    async def query_transaction(self, transaction_id: str, real_state: str) -> Dict[str, Any]:
        await asyncio.sleep(random.uniform(0.01, 0.2))

        if random.random() < 0.05:
            raise RuntimeError("CashVoucher: service error 503")

        state = STATE_MAP.get(real_state, "ERROR")
        rfc2822_timestamp = formatdate(localtime=True)

        return {
            "voucher_ref": transaction_id,
            "state": state,
            "issued_at": rfc2822_timestamp,
            "issuer": "CashVoucher",
            "voucher_number": f"CV{random.randint(10000, 99999)}",
            "valid": state == "PAID",
        }
