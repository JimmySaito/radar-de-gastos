"""Conector de Anthropic (Claude) basado en la Usage and Cost Analytics API (beta).

Usa el SDK oficial ``anthropic`` (``client.beta.organization.analytics``), que envía
los encabezados de versión/beta y pagina automáticamente. Requiere una Admin API key
(``sk-ant-admin...``) o una key con scope ``read:analytics`` en Claude Enterprise.

Notas del proveedor que condicionan el diseño:
- ``amount`` del endpoint /cost es un decimal en **centavos** de USD.
- Una solicitud cubre como máximo 31 días; los datos llegan con 4-24 h de retraso y
  pueden revisarse, por eso el colector re-lee una ventana móvil (lookback).
- Agrupar por ``actor`` atribuye cada request a un usuario, service account o API key:
  la base del showback por persona.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, cast

from usage_hub.connectors._util import RecordBuilder, midnight_utc, to_date, to_decimal
from usage_hub.connectors.registry import register
from usage_hub.domain.models import ConnectorConfig, Provider, UsageRecord
from usage_hub.domain.ports import CollectionWindow

CENTS = Decimal(100)
MAX_DAYS_PER_REQUEST = 31
GROUP_BY = ["actor", "model"]


class _AnalyticsListing(Protocol):
    def list(self, **kwargs: Any) -> Iterable[Any]: ...


def actor_principal(actor: Any) -> str:
    """Normaliza el objeto ``actor`` a un identificador estable ``tipo:id``."""
    if actor is None:
        return "unattributed"
    kind = str(_get(actor, "type") or "actor").removesuffix("_actor")
    for attr in ("user_id", "service_account_id", "api_key_id", "id", "email_address"):
        value = _get(actor, attr)
        if value:
            return f"{kind}:{value}"
    return kind


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


class AnthropicAnalyticsConnector:
    provider = Provider.ANTHROPIC

    def __init__(self, usage_api: _AnalyticsListing, cost_api: _AnalyticsListing) -> None:
        self._usage = usage_api
        self._cost = cost_api

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        builder = RecordBuilder(tenant_id, self.provider)
        for chunk in _chunks(window):
            params = {
                "starting_at": midnight_utc(chunk.start).isoformat().replace("+00:00", "Z"),
                "ending_at": midnight_utc(chunk.end).isoformat().replace("+00:00", "Z"),
                "time_bucket_width": "1d",
                "group_by": GROUP_BY,
                "limit": 1000,
            }
            for row in self._usage.list(**params):
                self._add_usage(builder, row)
            for row in self._cost.list(**params):
                self._add_cost(builder, row)
        return list(builder.build(keep=lambda d: window.start <= d < window.end))

    @staticmethod
    def _add_usage(builder: RecordBuilder, row: Any) -> None:
        group, usage = _get(row, "group"), _get(row, "usage")
        cache_creation = _get(usage, "cache_creation")
        cache_write = 0
        if cache_creation is not None:
            cache_write = int(_get(cache_creation, "ephemeral_1h_input_tokens") or 0) + int(
                _get(cache_creation, "ephemeral_5m_input_tokens") or 0
            )
        builder.add(
            to_date(_get(row, "starting_at")),
            str(_get(group, "model") or "unknown"),
            actor_principal(_get(group, "actor")),
            input_tokens=int(_get(usage, "uncached_input_tokens") or 0) + cache_write,
            output_tokens=int(_get(usage, "output_tokens") or 0),
            cached_tokens=int(_get(usage, "cache_read_input_tokens") or 0),
            requests=int(_get(usage, "requests") or 0),
        )

    @staticmethod
    def _add_cost(builder: RecordBuilder, row: Any) -> None:
        group, cost = _get(row, "group"), _get(row, "cost")
        builder.add(
            to_date(_get(row, "starting_at")),
            str(_get(group, "model") or "platform"),
            actor_principal(_get(group, "actor")),
            cost=to_decimal(_get(cost, "amount")) / CENTS,
        )


def _chunks(window: CollectionWindow) -> Iterable[CollectionWindow]:
    from datetime import timedelta

    start: date = window.start
    while start < window.end:
        end = min(start + timedelta(days=MAX_DAYS_PER_REQUEST), window.end)
        yield CollectionWindow(start, end)
        start = end


@register(Provider.ANTHROPIC)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> AnthropicAnalyticsConnector:
    import anthropic

    client = anthropic.Anthropic(api_key=secret["admin_api_key"], max_retries=4, timeout=60.0)
    analytics = client.beta.organization.analytics
    return AnthropicAnalyticsConnector(
        usage_api=cast(_AnalyticsListing, analytics.usage),
        cost_api=cast(_AnalyticsListing, analytics.cost),
    )
