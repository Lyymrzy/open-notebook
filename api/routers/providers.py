"""
Providers Router

Exposes the provider registry (open_notebook/ai/provider_registry.py) so
clients can enumerate supported providers and their metadata instead of
keeping their own copies.

Endpoints:
- GET /providers - List all supported providers with metadata
"""

from typing import List

from fastapi import APIRouter

from api.credentials_service import check_env_configured
from api.models import ProviderInfoResponse
from api.routers.models import DISABLED_MODEL_TYPES
from open_notebook.ai.provider_registry import PROVIDERS

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=List[ProviderInfoResponse])
async def list_providers():
    """List all supported AI providers with their registry metadata."""
    # Audio (speech-to-text / text-to-speech) model types are disabled in this
    # fork - see api/routers/models.py. Advertising them here would offer the UI
    # modalities that model creation and discovery reject anyway.
    return [
        ProviderInfoResponse(
            name=spec.name,
            display_name=spec.display_name,
            modalities=[m for m in spec.modalities if m not in DISABLED_MODEL_TYPES],
            docs_url=spec.docs_url,
            env_configured=check_env_configured(spec.name),
        )
        for spec in PROVIDERS.values()
    ]
