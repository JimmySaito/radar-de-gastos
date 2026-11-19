"""Utilidades compartidas por los conectores."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from radar_gastos.domain.models import Provider, UsageRecord

COST_QUANTUM = Decimal("0.000001")


def to_date(value: object) -> date:
    """Acepta datetime, date, ISO-8601 (con 'Z') o epoch en segundos."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC).date()
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    raise TypeError(f"fecha no soportada: {value!r}")


def to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def midnight_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(slots=True)
class _Accumulator:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    requests: int = 0
    cost: Decimal = field(default_factory=lambda: Decimal(0))


class RecordBuilder:
    """Acumula filas parciales (p. ej. tokens de un endpoint y costo de otro) por clave
    natural y emite un ``UsageRecord`` por clave. Evita emitir duplicados de la misma
    clave, que en el ledger se pisarían entre sí."""

    def __init__(self, tenant_id: str, provider: Provider, currency: str = "USD") -> None:
        self._tenant_id = tenant_id
        self._provider = provider
        self._currency = currency
        self._rows: dict[tuple[date, str, str], _Accumulator] = {}

    def add(
        self,
        day: date,
        service: str,
        principal: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        requests: int = 0,
        cost: Decimal = Decimal(0),
    ) -> None:
        acc = self._rows.setdefault(
            (day, service or "unknown", principal or "unknown"), _Accumulator()
        )
        acc.input_tokens += input_tokens
        acc.output_tokens += output_tokens
        acc.cached_tokens += cached_tokens
        acc.requests += requests
        acc.cost += cost

    def build(self, keep: Callable[[date], bool] = lambda _d: True) -> Iterable[UsageRecord]:
        for (day, service, principal), acc in sorted(self._rows.items()):
            if not keep(day):
                continue
            # Se redondea ANTES de decidir: una línea de costo de fracciones de centavo (p. ej.
            # OpenAI "cache writes - 12hr") queda en 0 y no debe producir un registro inválido.
            # Los ajustes negativos (créditos) quedan fuera del modelo canónico (ver deuda técnica).
            cost = max(acc.cost.quantize(COST_QUANTUM), Decimal(0))
            if not any(
                (acc.input_tokens, acc.output_tokens, acc.cached_tokens, acc.requests, cost)
            ):
                continue
            yield UsageRecord(
                tenant_id=self._tenant_id,
                provider=self._provider,
                service=service,
                principal=principal,
                usage_date=day,
                input_tokens=acc.input_tokens,
                output_tokens=acc.output_tokens,
                cached_tokens=acc.cached_tokens,
                requests=acc.requests,
                cost=cost,
                currency=self._currency,
            )
