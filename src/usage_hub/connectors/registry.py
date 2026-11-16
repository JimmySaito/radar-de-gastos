"""Registro de fábricas de conectores (plug-in architecture)."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping

from usage_hub.domain.models import ConnectorConfig, Provider
from usage_hub.domain.ports import UsageConnector

ConnectorFactory = Callable[[ConnectorConfig, Mapping[str, str]], UsageConnector]

_REGISTRY: dict[Provider, ConnectorFactory] = {}

# Módulos que se importan bajo demanda: un Lambda que sólo usa OpenAI no paga el
# costo de arranque en frío de importar el SDK de Google.
_LAZY_MODULES: dict[Provider, str] = {
    Provider.ANTHROPIC: "usage_hub.connectors.anthropic_admin",
    Provider.OPENAI: "usage_hub.connectors.openai_admin",
    Provider.AWS: "usage_hub.connectors.aws_cost_explorer",
    Provider.GOOGLE: "usage_hub.connectors.google_billing",
    Provider.SYNTHETIC: "usage_hub.connectors.synthetic",
    Provider.CLAUDE_CODE: "usage_hub.connectors.claude_code_local",
    Provider.SUBSCRIPTION: "usage_hub.connectors.subscription",
}


class UnknownProviderError(LookupError):
    pass


def register(provider: Provider) -> Callable[[ConnectorFactory], ConnectorFactory]:
    def decorator(factory: ConnectorFactory) -> ConnectorFactory:
        _REGISTRY[provider] = factory
        return factory

    return decorator


def build_connector(config: ConnectorConfig, secret: Mapping[str, str]) -> UsageConnector:
    if config.provider not in _REGISTRY and config.provider in _LAZY_MODULES:
        importlib.import_module(_LAZY_MODULES[config.provider])
    try:
        factory = _REGISTRY[config.provider]
    except KeyError as exc:
        raise UnknownProviderError(f"no hay conector registrado para {config.provider}") from exc
    return factory(config, secret)


def registered_providers() -> frozenset[Provider]:
    return frozenset(_REGISTRY) | frozenset(_LAZY_MODULES)
