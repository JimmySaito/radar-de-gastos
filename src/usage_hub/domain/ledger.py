"""Lógica pura del ledger de consumo: cálculo de deltas para agregados.

Problema: SQS entrega *al menos una vez* y los proveedores *revisan* los datos de un
día durante horas. Sumar ciegamente cada lectura duplicaría el costo. La solución es
tratar cada hecho como un upsert por clave natural y aplicar a los agregados sólo la
**diferencia** entre la versión anterior y la nueva. Reprocesar el mismo mensaje
produce delta cero (idempotencia), y una revisión produce exactamente el ajuste.

Este módulo no conoce DynamoDB: calcula *qué* cambiar; el adaptador decide *cómo*
aplicarlo de forma atómica (TransactWriteItems con control de concurrencia optimista).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from usage_hub.domain.models import Attribution, Metrics, UsageRecord


class Dimension(StrEnum):
    """Dimensiones pre-agregadas que consume el dashboard (patrón CQRS: modelo de lectura)."""

    MEMBER = "member"
    TEAM = "team"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class AggregateKey:
    """Identifica una celda del cubo diario: (fecha, proveedor, dimensión, valor)."""

    usage_date: str
    provider: str
    dimension: Dimension
    value: str


@dataclass(frozen=True, slots=True)
class StoredFact:
    """Versión persistida de un hecho junto con la atribución usada al escribirlo."""

    record: UsageRecord
    attribution: Attribution
    version: int


def aggregate_keys(record: UsageRecord, attribution: Attribution) -> dict[Dimension, AggregateKey]:
    day, provider = record.usage_date.isoformat(), record.provider.value
    return {
        Dimension.MEMBER: AggregateKey(day, provider, Dimension.MEMBER, attribution.member),
        Dimension.TEAM: AggregateKey(day, provider, Dimension.TEAM, attribution.team),
        Dimension.SERVICE: AggregateKey(day, provider, Dimension.SERVICE, record.service),
    }


def compute_deltas(
    previous: StoredFact | None, new: UsageRecord, attribution: Attribution
) -> dict[AggregateKey, Metrics]:
    """Devuelve el ajuste por celda agregada necesario para pasar de ``previous`` a ``new``.

    - Primera escritura: +new en cada celda.
    - Reescritura idéntica: {} (no-op).
    - Revisión de métricas: +(new - old) en cada celda.
    - Cambio de atribución (el mapeo principal→persona cambió): -old en las celdas
      antiguas y +new en las nuevas. Las celdas que coinciden se fusionan para no
      tocar el mismo ítem dos veces en una transacción (restricción de DynamoDB).
    """
    if previous is not None and previous.record.natural_key != new.natural_key:
        raise ValueError("compute_deltas requiere el mismo hecho (misma clave natural)")

    deltas: defaultdict[AggregateKey, Metrics] = defaultdict(Metrics)
    if previous is not None:
        old_metrics = Metrics.of(previous.record)
        for key in aggregate_keys(previous.record, previous.attribution).values():
            deltas[key] = deltas[key] - old_metrics

    new_metrics = Metrics.of(new)
    for key in aggregate_keys(new, attribution).values():
        deltas[key] = deltas[key] + new_metrics

    return {key: delta for key, delta in deltas.items() if not delta.is_zero}


def is_noop(previous: StoredFact | None, new: UsageRecord, attribution: Attribution) -> bool:
    return (
        previous is not None
        and previous.record.content_hash == new.content_hash
        and previous.attribution == attribution
    )
