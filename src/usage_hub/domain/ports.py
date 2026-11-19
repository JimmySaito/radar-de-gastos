"""Puertos (interfaces) de la arquitectura hexagonal.

El dominio y los casos de uso dependen de estas abstracciones; los adaptadores de AWS
(DynamoDB, Secrets Manager, SQS, SNS) y los de memoria (pruebas, demo local) las
implementan. Esto hace que el núcleo sea testeable sin la nube (Testability) y que
cambiar de almacenamiento no afecte a la lógica (Modifiability).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from usage_hub.domain.ledger import AggregateKey, Dimension
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
from usage_hub.domain.recurring import RecurringExpense


class WriteOutcome(StrEnum):
    CREATED = "created"
    REVISED = "revised"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class CollectionWindow:
    start: date  # inclusivo
    end: date  # exclusivo

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("la ventana debe tener end > start")

    def days(self) -> Iterator[date]:
        from datetime import timedelta

        current = self.start
        while current < self.end:
            yield current
            current += timedelta(days=1)


@runtime_checkable
class UsageConnector(Protocol):
    """Strategy/Adapter: traduce la API de un proveedor al modelo canónico."""

    provider: Provider

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]: ...


class SecretProvider(Protocol):
    def get_secret(self, secret_ref: str) -> Mapping[str, str]: ...


class SecretStore(SecretProvider, Protocol):
    """Custodia de credenciales de conectores. Devuelve una referencia opaca (ARN o id)
    que es lo único que se guarda en la configuración; el valor nunca sale hacia la UI."""

    def put_secret(self, tenant_id: str, name: str, values: Mapping[str, str]) -> str: ...

    def delete_secret(self, secret_ref: str) -> None: ...


class UsageRepository(Protocol):
    """Ledger de hechos + modelo de lectura agregado."""

    def upsert(self, record: UsageRecord, attribution: Attribution) -> WriteOutcome: ...

    def query_aggregates(
        self, tenant_id: str, start: date, end: date, dimension: Dimension
    ) -> Mapping[AggregateKey, Metrics]: ...


class MappingRepository(Protocol):
    def resolve(self, tenant_id: str, provider: Provider, principal: str) -> Attribution: ...

    def put_mapping(self, tenant_id: str, mapping: PrincipalMapping) -> None: ...

    def list_mappings(self, tenant_id: str) -> Sequence[PrincipalMapping]: ...

    def delete_mapping(self, tenant_id: str, provider: Provider, principal: str) -> None: ...


class ConfigRepository(Protocol):
    def list_connectors(self, tenant_id: str | None = None) -> Sequence[ConnectorConfig]: ...

    def put_connector(self, config: ConnectorConfig) -> None: ...

    def delete_connector(self, tenant_id: str, connector_id: str) -> None: ...

    def record_sync(self, tenant_id: str, connector_id: str, status: ConnectorStatus) -> None: ...

    def list_statuses(self, tenant_id: str) -> Mapping[str, ConnectorStatus]: ...

    def list_budgets(self, tenant_id: str) -> Sequence[Budget]: ...

    def put_budget(self, budget: Budget) -> None: ...

    def delete_budget(self, tenant_id: str, team: str, provider: Provider | None) -> None: ...


class RecurringRepository(Protocol):
    """Gastos recurrentes por dueño: ``user:{sub}`` (personal) o ``tenant:{id}`` (empresa)."""

    def list_expenses(self, owner: str) -> Sequence[RecurringExpense]: ...

    def put_expense(self, owner: str, expense: RecurringExpense) -> None: ...

    def delete_expense(self, owner: str, expense_id: str) -> None: ...


class WorkQueue(Protocol):
    def send_batch(self, messages: Sequence[Mapping[str, object]]) -> int: ...


class AlertPublisher(Protocol):
    def publish(self, subject: str, message: Mapping[str, object]) -> None: ...
