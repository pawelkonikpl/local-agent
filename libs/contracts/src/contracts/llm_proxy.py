"""The llm-proxy API as api calls it.

`POST /v1/messages` takes an Anthropic Messages request and streams Anthropic Messages SSE back,
whichever provider serves the model. `GET /v1/models` answers `ModelsResponse`. Both need the
internal bearer token (`contracts.auth`); `/v1/messages` also needs the two id headers below, for
metering.
"""

from pydantic import BaseModel

MESSAGES_PATH = "/v1/messages"
MODELS_PATH = "/v1/models"
USER_ID_HEADER = "X-Local-Agent-User-Id"
SESSION_ID_HEADER = "X-Local-Agent-Session-Id"


class ModelInfo(BaseModel):
    """A model a provider serves, as offered to the chat's model picker."""

    id: str
    # Levels of Anthropic's `output_config.effort` the model accepts; empty = no reasoning setting.
    reasoning_efforts: list[str] = []


class ModelsResponse(BaseModel):
    models: list[ModelInfo]
