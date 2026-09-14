"""Módulo de Provedores Educacionais (LMS Providers) do LumiBot.

Exporta interfaces e adaptadores concretos para suporte Multi-LMS (Moodle, Canvas, etc.).
"""

from typing import Dict, Optional

from config.settings import settings
from src.providers.base import BaseLMSProvider
from src.providers.moodle import MoodleAdapter
from src.providers.canvas import (
    CanvasAdapter,
    CanvasSubmitter,
    extract_canvas_ids,
    CanvasError,
    CanvasAuthenticationError,
    CanvasRateLimitError,
    CanvasAPIError,
)

__all__ = [
    "BaseLMSProvider",
    "MoodleAdapter",
    "CanvasAdapter",
    "CanvasSubmitter",
    "extract_canvas_ids",
    "CanvasError",
    "CanvasAuthenticationError",
    "CanvasRateLimitError",
    "CanvasAPIError",
    "get_lms_provider",
    "get_available_providers",
]


def get_lms_provider(name: Optional[str] = None) -> BaseLMSProvider:
    """Factory para instanciar o provedor de LMS configurado ou solicitado.

    Args:
        name: Nome do provedor ('moodle' ou 'canvas'). Se None, lê de settings.LMS_PROVIDER.

    Returns:
        BaseLMSProvider: Instância concreta do adaptador configurado.
    """
    provider_name = (name or getattr(settings, "LMS_PROVIDER", "moodle")).strip().lower()

    if provider_name == "canvas":
        return CanvasAdapter()
    return MoodleAdapter()


def get_available_providers() -> Dict[str, BaseLMSProvider]:
    """Retorna instâncias de todos os adaptadores suportados no ecossistema."""
    return {
        "moodle": MoodleAdapter(),
        "canvas": CanvasAdapter(),
    }
