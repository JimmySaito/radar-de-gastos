"""Caché corta de lecturas de agregados (lado *Query* de CQRS, ADR-0007).

Bajo carga, el dashboard repite las mismas consultas (mismo tenant, rango y dimensión) muchas
veces por segundo. Los datos de los proveedores ya llegan con horas de retraso y la ingesta es
asíncrona, así que servir una lectura con unos segundos de antigüedad no cambia lo que el usuario
puede saber, y evita recorrer DynamoDB en cada petición. La escritura pasa directo.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import date

from radar_gastos.domain.ledger import AggregateKey, Dimension
from radar_gastos.domain.models import Attribution, Metrics, UsageRecord
from radar_gastos.domain.ports import UsageRepository, WriteOutcome

CacheKey = tuple[str, date, date, Dimension]


class CachedUsageRepository:
    """Envuelve un ``UsageRepository``: lecturas con TTL, escrituras sin cambios."""

    def __init__(
        self,
        inner: UsageRepository,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 256,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[CacheKey, tuple[float, Mapping[AggregateKey, Metrics]]] = {}

    def upsert(self, record: UsageRecord, attribution: Attribution) -> WriteOutcome:
        return self._inner.upsert(record, attribution)

    def query_aggregates(
        self, tenant_id: str, start: date, end: date, dimension: Dimension
    ) -> Mapping[AggregateKey, Metrics]:
        key = (tenant_id, start, end, dimension)
        now = self._clock()
        with self._lock:
            hit = self._entries.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        value = self._inner.query_aggregates(tenant_id, start, end, dimension)
        with self._lock:
            if len(self._entries) >= self._max:  # acotada: descarta la entrada más vieja
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
            self._entries[key] = (now, value)
        return value
