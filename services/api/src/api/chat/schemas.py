import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    approval_mode: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    sequence_number: int
    role: str
    content: list[dict]
    created_at: datetime


class CreateMessageRequest(BaseModel):
    content: str = Field(min_length=1)
    # One of `GET /models`; `None` means the configured default.
    model: str | None = None
    # One of the chosen model's `reasoning_efforts`; `None` means the provider's default.
    reasoning_effort: str | None = None


class ModelOut(BaseModel):
    id: str
    # Empty when the model offers no reasoning setting.
    reasoning_efforts: list[str]


class ModelsOut(BaseModel):
    models: list[ModelOut]
    default: str


class SessionUsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
