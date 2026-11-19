"""Registro de fábricas de conectores (plug-in architecture)."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping

from radar_gastos.domain.models import ConnectorConfig, Provider
from radar_gastos.domain.ports import UsageConnector

ConnectorFactory = Callable[[ConnectorConfig, Mapping[str, str]], UsageConnector]

_REGISTRY: dict[Provider, ConnectorFactory] = {}

# Módulos que se importan bajo demanda: un Lambda que sólo usa OpenAI no paga el
# costo de arranque en frío de importar el SDK de Google.
_LAZY_MODULES: dict[Provider, str] = {
    Provider.ANTHROPIC: "radar_gastos.connectors.anthropic_admin",
    Provider.OPENAI: "radar_gastos.connectors.openai_admin",
    Provider.AWS: "radar_gastos.connectors.aws_cost_explorer",
    Provider.GOOGLE: "radar_gastos.connectors.google_billing",
    Provider.SYNTHETIC: "radar_gastos.connectors.synthetic",
    Provider.CLAUDE_CODE: "radar_gastos.connectors.claude_code_local",
    Provider.SUBSCRIPTION: "radar_gastos.connectors.subscription",
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
