"""Costos fijos de suscripciones (Claude Pro/Max/Team, ChatGPT Plus/Team, Gemini...).

Las suscripciones no exponen tokens ni API de uso, pero sí son gasto real. Este conector
prorratea la tarifa mensual por día (tarifa / días del mes) para que aparezca en las
mismas series, presupuestos y rankings que el consumo variable. Los registros usan el
proveedor real (p. ej. ``anthropic``) y el principal ``subscription:{persona}``.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from usage_hub.connectors._util import RecordBuilder
from usage_hub.connectors.registry import register
from usage_hub.domain.models import ConnectorConfig, Provider, UsageRecord
from usage_hub.domain.ports import CollectionWindow

VENDORS = {
    "anthropic": Provider.ANTHROPIC,
    "openai": Provider.OPENAI,
    "google": Provider.GOOGLE,
    "aws": Provider.AWS,
    "custom": Provider.CUSTOM,
}


class SubscriptionConnector:
    provider = Provider.SUBSCRIPTION

    def __init__(
        self,
        vendor: Provider,
        plan: str,
        monthly_cost: Decimal,
        member: str,
        start: date | None = None,
    ) -> None:
        if monthly_cost <= 0:
            raise ValueError("la tarifa mensual debe ser mayor que cero")
        self._vendor = vendor
        self._plan = plan
        self._monthly = monthly_cost
        self._member = member
        self._start = start

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        builder = RecordBuilder(tenant_id, self._vendor)
        for day in window.days():
            if self._start and day < self._start:
                continue
            days_in_month = calendar.monthrange(day.year, day.month)[1]
            builder.add(
                day,
                f"{self._plan} (suscripción)",
                f"subscription:{self._member}",
                cost=self._monthly / days_in_month,
            )
        return list(builder.build())


@register(Provider.SUBSCRIPTION)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> SubscriptionConnector:
    options = config.options
    try:
        monthly = Decimal(options.get("monthly_cost", "0").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("la tarifa mensual debe ser un número") from exc
    start = date.fromisoformat(options["start_date"]) if options.get("start_date") else None
    return SubscriptionConnector(
        vendor=VENDORS.get(options.get("vendor", "anthropic"), Provider.CUSTOM),
        plan=options.get("plan", "Suscripción"),
        monthly_cost=monthly,
        member=options.get("member", "sin-asignar"),
        start=start,
    )
