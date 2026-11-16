"""Adaptadores en memoria: pruebas unitarias, demo local y benchmarks del dominio."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date

from usage_hub.domain.ledger import AggregateKey, Dimension, StoredFact, compute_deltas, is_noop
from usage_hub.domain.models import (
    Attribution,
    Budget,
    ConnectorConfig,
    Metrics,
    PrincipalMapping,
    Provider,
    UsageRecord,
)
from usage_hub.domain.ports import WriteOutcome


class InMemoryUsageRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.facts: dict[str, StoredFact] = {}
        self.aggregates: dict[tuple[str, AggregateKey], Metrics] = defaultdict(Metrics)

    def upsert(self, record: UsageRecord, attribution: Attribution) -> WriteOutcome:
        with self._lock:
            previous = self.facts.get(record.natural_key)
            if is_noop(previous, record, attribution):
                return WriteOutcome.UNCHANGED
            for key, delta in compute_deltas(previous, record, attribution).items():
                self.aggregates[(record.tenant_id, key)] = (
                    self.aggregates[(record.tenant_id, key)] + delta
                )
            version = previous.version + 1 if previous else 1
            self.facts[record.natural_key] = StoredFact(record, attribution, version)
            return WriteOutcome.REVISED if previous else WriteOutcome.CREATED

    def query_aggregates(
        self, tenant_id: str, start: date, end: date, dimension: Dimension
    ) -> Mapping[AggregateKey, Metrics]:
        lo, hi = start.isoformat(), end.isoformat()
        return {
            key: metrics
            for (tenant, key), metrics in self.aggregates.items()
            if tenant == tenant_id
            and key.dimension == dimension
            and lo <= key.usage_date < hi
            and not metrics.is_zero
        }


class InMemoryMappingRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, Provider, str], PrincipalMapping] = {}

    def resolve(self, tenant_id: str, provider: Provider, principal: str) -> Attribution:
        mapping = self._items.get((tenant_id, provider, principal))
        if mapping is None:
            return Attribution(member=principal)
        return Attribution(member=mapping.member, team=mapping.team)

    def put_mapping(self, tenant_id: str, mapping: PrincipalMapping) -> None:
        self._items[(tenant_id, mapping.provider, mapping.principal)] = mapping

    def list_mappings(self, tenant_id: str) -> Sequence[PrincipalMapping]:
        return [m for (t, _, _), m in self._items.items() if t == tenant_id]


class InMemoryConfigRepository:
    def __init__(self) -> None:
        self.connectors: dict[tuple[str, str], ConnectorConfig] = {}
        self.budgets: dict[tuple[str, str, str], Budget] = {}
        self.sent_alerts: set[str] = set()

    def list_connectors(self, tenant_id: str | None = None) -> Sequence[ConnectorConfig]:
        return [c for (t, _), c in self.connectors.items() if tenant_id in (None, t)]

    def put_connector(self, config: ConnectorConfig) -> None:
        self.connectors[(config.tenant_id, config.connector_id)] = config

    def list_budgets(self, tenant_id: str) -> Sequence[Budget]:
        return [b for (t, _, _), b in self.budgets.items() if t == tenant_id]

    def put_budget(self, budget: Budget) -> None:
        self.budgets[(budget.tenant_id, budget.team, budget.provider or "*")] = budget

    def mark_alert_sent(self, alert_id: str) -> bool:
        if alert_id in self.sent_alerts:
            return False
        self.sent_alerts.add(alert_id)
        return True


class InMemoryQueue:
    def __init__(self) -> None:
        self.messages: list[Mapping[str, object]] = []

    def send_batch(self, messages: Sequence[Mapping[str, object]]) -> int:
        self.messages.extend(messages)
        return len(messages)


class InMemoryAlerts:
    def __init__(self) -> None:
        self.published: list[tuple[str, Mapping[str, object]]] = []

    def publish(self, subject: str, message: Mapping[str, object]) -> None:
        self.published.append((subject, message))


class StaticSecrets:
    def __init__(self, secrets: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def get_secret(self, secret_ref: str) -> Mapping[str, str]:
        return self._secrets.get(secret_ref, {})
