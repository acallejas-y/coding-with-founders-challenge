from pydantic import BaseModel, validator
from typing import List


class BulkRecoverRequest(BaseModel):
    transaction_ids: List[str]

    @validator("transaction_ids")
    def validate_ids(cls, v):
        if not v:
            raise ValueError("transaction_ids cannot be empty")
        if len(v) > 500:
            raise ValueError("Maximum 500 transaction IDs per request")
        return v
