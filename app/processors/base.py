from abc import ABC, abstractmethod
from typing import Dict, Any


class BaseProcessor(ABC):
    """Abstract base for all payment processor mocks."""

    @abstractmethod
    async def query_transaction(self, transaction_id: str, real_state: str) -> Dict[str, Any]:
        """
        Query the processor for the current state of a transaction.
        real_state is the ground truth from the DB, used to simulate the response.
        Returns the processor's raw response dict.
        """
        pass

    @property
    @abstractmethod
    def processor_name(self) -> str:
        pass
