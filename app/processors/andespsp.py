import asyncio
import random
from datetime import datetime
from typing import Dict, Any
from app.processors.base import BaseProcessor


STATE_MAP = {
    "approved": "aprobada",
    "declined": "rechazada",
    "pending": "pendiente",
    "unknown": "desconocido",
}


class AndesPSPProcessor(BaseProcessor):
    """
    AndesPSP mock.
    Status field: `transaction_state`
    Status values: aprobada / rechazada / pendiente / desconocido
    Timestamp format: DD/MM/YYYY HH:MM:SS
    Error rate: ~5%
    """

    @property
    def processor_name(self) -> str:
        return "andespsp"

    async def query_transaction(self, transaction_id: str, real_state: str) -> Dict[str, Any]:
        await asyncio.sleep(random.uniform(0.01, 0.2))

        if random.random() < 0.05:
            raise RuntimeError("AndesPSP: error de conexión")

        transaction_state = STATE_MAP.get(real_state, "desconocido")
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        return {
            "transaccion_id": transaction_id,
            "transaction_state": transaction_state,
            "fecha_hora": timestamp,
            "procesador": "AndesPSP",
            "codigo_respuesta": "00" if transaction_state == "aprobada" else "99",
            "mensaje": "Aprobado" if transaction_state == "aprobada" else "Ver codigo",
        }
