import httpx
from pydantic import BaseModel, ValidationError

from api.chat.schemas import ModelOut, ModelsOut
from api.config import settings


class ModelCatalogUnavailableError(Exception):
    """llm-proxy couldn't say which models its providers serve."""


class _ProxyModels(BaseModel):
    models: list[ModelOut]


async def fetch_models(client: httpx.AsyncClient) -> ModelsOut:
    """The models llm-proxy's providers serve (`GET /v1/models`, which asks each provider and
    caches the answer). `settings.chat_model` goes first and is the default when a provider
    lists it; otherwise the first listed model is, so the default is always one on offer."""
    try:
        response = await client.get(
            "/v1/models", headers={"Authorization": f"Bearer {settings.internal_proxy_token}"}
        )
        response.raise_for_status()
        models = _ProxyModels.model_validate(response.json()).models
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise ModelCatalogUnavailableError(f"Could not list models from llm-proxy: {exc}") from exc
    if not models:
        raise ModelCatalogUnavailableError("llm-proxy listed no models")
    default = next((model for model in models if model.id == settings.chat_model), models[0])
    return ModelsOut(models=[default, *(model for model in models if model is not default)], default=default.id)
