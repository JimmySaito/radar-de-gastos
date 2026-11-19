"""Adaptadores en memoria: pruebas unitarias, demo local y benchmarks del dominio."""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date

from usage_hub.domain.ledger import AggregateKey, Dimension, StoredFact, compute_deltas, is_noop
from usage_hub.domain.models import (
    Attribution,
    Budget,
    ConnectorConfig,
    ConnectorStatus,
    Metrics,
    PrincipalMapping,
    Provider,
    UsageRecord,
)
from usage_hub.domain.ports import WriteOutcome
from usage_hub.domain.recurring import Profile, RecurringExpense


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
        with self._lock:
            items = list(self.aggregates.items())
        return {
            key: metrics
            for (tenant, key), metrics in items
            if tenant == tenant_id
            and key.dimension == dimension
            and lo <= key.usage_date < hi
            and not metrics.is_zero
        }


class InMemoryMappingRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[tuple[str, Provider, str], PrincipalMapping] = {}

    def resolve(self, tenant_id: str, provider: Provider, principal: str) -> Attribution:
        mapping = self._items.get((tenant_id, provider, principal))
        if mapping is None:
            return Attribution(member=principal)
        return Attribution(member=mapping.member, team=mapping.team)

    def put_mapping(self, tenant_id: str, mapping: PrincipalMapping) -> None:
        with self._lock:
            self._items[(tenant_id, mapping.provider, mapping.principal)] = mapping
            self._changed()

    def delete_mapping(self, tenant_id: str, provider: Provider, principal: str) -> None:
        with self._lock:
            self._items.pop((tenant_id, provider, principal), None)
            self._changed()

    def list_mappings(self, tenant_id: str) -> Sequence[PrincipalMapping]:
        return [m for (t, _, _), m in self._items.items() if t == tenant_id]

    def _changed(self) -> None:
        """Gancho para adaptadores con persistencia (ver ``adapters.localfs``)."""


class InMemoryConfigRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.connectors: dict[tuple[str, str], ConnectorConfig] = {}
        self.statuses: dict[tuple[str, str], ConnectorStatus] = {}
        self.budgets: dict[tuple[str, str, str], Budget] = {}
        self.sent_alerts: set[str] = set()

    def list_connectors(self, tenant_id: str | None = None) -> Sequence[ConnectorConfig]:
        return [c for (t, _), c in self.connectors.items() if tenant_id in (None, t)]

    def put_connector(self, config: ConnectorConfig) -> None:
        with self._lock:
            self.connectors[(config.tenant_id, config.connector_id)] = config
            self._changed()

    def delete_connector(self, tenant_id: str, connector_id: str) -> None:
        with self._lock:
            self.connectors.pop((tenant_id, connector_id), None)
            self.statuses.pop((tenant_id, connector_id), None)
            self._changed()

    def record_sync(self, tenant_id: str, connector_id: str, status: ConnectorStatus) -> None:
        with self._lock:
            if (tenant_id, connector_id) in self.connectors:
                self.statuses[(tenant_id, connector_id)] = status
                self._changed()

    def list_statuses(self, tenant_id: str) -> Mapping[str, ConnectorStatus]:
        return {cid: s for (t, cid), s in self.statuses.items() if t == tenant_id}

    def list_budgets(self, tenant_id: str) -> Sequence[Budget]:
        return [b for (t, _, _), b in self.budgets.items() if t == tenant_id]

    def put_budget(self, budget: Budget) -> None:
        with self._lock:
            self.budgets[(budget.tenant_id, budget.team, budget.provider or "*")] = budget
            self._changed()

    def delete_budget(self, tenant_id: str, team: str, provider: Provider | None) -> None:
        with self._lock:
            self.budgets.pop((tenant_id, team, provider or "*"), None)
            self._changed()

    def mark_alert_sent(self, alert_id: str) -> bool:
        with self._lock:
            if alert_id in self.sent_alerts:
                return False
            self.sent_alerts.add(alert_id)
            self._changed()
            return True

    def _changed(self) -> None:
        """Gancho para adaptadores con persistencia (ver ``adapters.localfs``)."""


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
    """Almacén de secretos en memoria (pruebas). Implementa ``SecretStore``."""

    def __init__(self, secrets: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._lock = threading.RLock()
        self._secrets: dict[str, dict[str, str]] = {k: dict(v) for k, v in (secrets or {}).items()}

    def get_secret(self, secret_ref: str) -> Mapping[str, str]:
        return dict(self._secrets.get(secret_ref, {}))

    def put_secret(self, tenant_id: str, name: str, values: Mapping[str, str]) -> str:
        with self._lock:
            ref = f"local:{tenant_id}/{name}/{uuid.uuid4().hex[:8]}"
            self._secrets[ref] = dict(values)
            self._changed()
            return ref

    def delete_secret(self, secret_ref: str) -> None:
        with self._lock:
            self._secrets.pop(secret_ref, None)
            self._changed()

    def _changed(self) -> None:
        """Gancho para adaptadores con persistencia (ver ``adapters.localfs``)."""


class InMemoryRecurringRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, RecurringExpense]] = defaultdict(dict)
        self._profiles: dict[str, dict[str, Profile]] = defaultdict(dict)

    def list_expenses(self, owner: str) -> Sequence[RecurringExpense]:
        return sorted(self._items.get(owner, {}).values(), key=lambda e: e.name.lower())

    def put_expense(self, owner: str, expense: RecurringExpense) -> None:
        with self._lock:
            self._items[owner][expense.expense_id] = expense
            self._changed()

    def delete_expense(self, owner: str, expense_id: str) -> None:
        with self._lock:
            self._items.get(owner, {}).pop(expense_id, None)
            self._changed()

    def list_profiles(self, owner: str) -> Sequence[Profile]:
        return sorted(self._profiles.get(owner, {}).values(), key=lambda p: p.name.lower())

    def put_profile(self, owner: str, profile: Profile) -> None:
        with self._lock:
            self._profiles[owner][profile.profile_id] = profile
            self._changed()

    def delete_profile(self, owner: str, profile_id: str) -> None:
        with self._lock:
            self._profiles.get(owner, {}).pop(profile_id, None)
            self._changed()

    def _changed(self) -> None:
        """Gancho para adaptadores con persistencia (ver ``adapters.localfs``)."""
