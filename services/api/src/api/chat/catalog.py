from api.chat.llm_proxy import LlmProxy
from api.chat.schemas import ModelsOut
from api.config import settings


async def fetch_models(llm: LlmProxy) -> ModelsOut:
    """The models llm-proxy's providers serve. `settings.chat_model` goes first and is the default
    when a provider lists it; otherwise the first listed model is, so the default is always one
    on offer. Raises `ModelCatalogUnavailableError`."""
    models = await llm.list_models()
    default = next((model for model in models if model.id == settings.chat_model), models[0])
    return ModelsOut(models=[default, *(model for model in models if model is not default)], default=default.id)
