"""Casos de uso de ingesta: planificación (fan-out), recolección (pull) e ingesta (push)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from radar_gastos.connectors.registry import build_connector
from radar_gastos.domain.models import ConnectorConfig, ConnectorStatus, UsageRecord
from radar_gastos.domain.ports import (
    CollectionWindow,
    ConfigRepository,
    MappingRepository,
    SecretProvider,
    UsageConnector,
    UsageRepository,
    WorkQueue,
    WriteOutcome,
)

MAX_RECORDS_PER_MESSAGE = 200


class CollectTask(BaseModel):
    """Mensaje SQS de tipo *pull*: recolectar un conector para una ventana."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(default="collect", pattern="^collect$")
    tenant_id: str
    connector_id: str
    start: date
    end: date


class RecordsTask(BaseModel):
    """Mensaje SQS de tipo *push*: registros ya normalizados (API de ingesta)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(default="records", pattern="^records$")
    records: list[UsageRecord] = Field(min_length=1, max_length=MAX_RECORDS_PER_MESSAGE)


_TASK_ADAPTER: TypeAdapter[CollectTask | RecordsTask] = TypeAdapter(CollectTask | RecordsTask)


def parse_task(body: str | bytes | Mapping[str, Any]) -> CollectTask | RecordsTask:
    if isinstance(body, Mapping):
        return _TASK_ADAPTER.validate_python(body)
    return _TASK_ADAPTER.validate_json(body)


@dataclass(slots=True)
class IngestionStats:
    outcomes: Counter[WriteOutcome] = field(default_factory=Counter)

    @property
    def written(self) -> int:
        return self.outcomes[WriteOutcome.CREATED] + self.outcomes[WriteOutcome.REVISED]

    def as_dict(self) -> dict[str, int]:
        return {o.value: self.outcomes[o] for o in WriteOutcome}


class SchedulePlanner:
    """Fan-out: un mensaje por conector habilitado. La ventana móvil (lookback) re-lee
    los días recientes porque los proveedores revisan datos hasta 24 h después."""

    def __init__(
        self, configs: ConfigRepository, queue: WorkQueue, today: Callable[[], date]
    ) -> None:
        self._configs = configs
        self._queue = queue
        self._today = today

    def plan(self) -> int:
        end = self._today() + timedelta(days=1)
        tasks = [
            CollectTask(
                tenant_id=c.tenant_id,
                connector_id=c.connector_id,
                start=end - timedelta(days=c.lookback_days),
                end=end,
            ).model_dump(mode="json")
            for c in self._configs.list_connectors()
            if c.enabled
        ]
        return self._queue.send_batch(tasks) if tasks else 0


class UsageIngestor:
    """Escribe registros en el ledger resolviendo la atribución (principal → persona/equipo)."""

    def __init__(self, repository: UsageRepository, mappings: MappingRepository) -> None:
        self._repository = repository
        self._mappings = mappings

    def ingest(self, records: Iterable[UsageRecord]) -> IngestionStats:
        stats = IngestionStats()
        for record in records:
            attribution = self._mappings.resolve(
                record.tenant_id, record.provider, record.principal
            )
            stats.outcomes[self._repository.upsert(record, attribution)] += 1
        return stats


ConnectorBuilder = Callable[[ConnectorConfig, Mapping[str, str]], UsageConnector]


class UsageCollector:
    """Ejecuta una tarea: pull desde el proveedor o registros push, y los ingiere."""

    def __init__(
        self,
        configs: ConfigRepository,
        secrets: SecretProvider,
        ingestor: UsageIngestor,
        connector_builder: ConnectorBuilder = build_connector,
    ) -> None:
        self._configs = configs
        self._secrets = secrets
        self._ingestor = ingestor
        self._build = connector_builder

    def handle(self, task: CollectTask | RecordsTask) -> IngestionStats:
        if isinstance(task, RecordsTask):
            return self._ingestor.ingest(task.records)
        config = self._find_config(task.tenant_id, task.connector_id)
        secret: Mapping[str, str] = {}
        try:
            if config.secret_arn:
                secret = self._secrets.get_secret(config.secret_arn)
            connector = self._build(config, secret)
            records = connector.fetch(task.tenant_id, CollectionWindow(task.start, task.end))
            stats = self._ingestor.ingest(r for r in records if r.tenant_id == task.tenant_id)
        except Exception as exc:
            self._record(task, ok=False, error=safe_error(exc, list(secret.values())))
            raise
        self._record(task, ok=True, stats=stats)
        return stats

    def _record(
        self,
        task: CollectTask,
        *,
        ok: bool,
        stats: IngestionStats | None = None,
        error: str | None = None,
    ) -> None:
        self._configs.record_sync(
            task.tenant_id,
            task.connector_id,
            ConnectorStatus(
                last_sync_at=datetime.now(tz=UTC),
                ok=ok,
                written=stats.written if stats else 0,
                unchanged=stats.outcomes[WriteOutcome.UNCHANGED] if stats else 0,
                error=error,
                window_start=task.start,
                window_end=task.end,
            ),
        )

    def _find_config(self, tenant_id: str, connector_id: str) -> ConnectorConfig:
        for config in self._configs.list_connectors(tenant_id):
            if config.connector_id == connector_id:
                return config
        raise LookupError(f"conector {connector_id} no existe para el tenant {tenant_id}")


MAX_ERROR_LENGTH = 300


def safe_error(exc: BaseException, secrets: Sequence[str] = ()) -> str:
    """Mensaje de error apto para mostrar en la UI: sin credenciales y acotado."""
    message = f"{type(exc).__name__}: {exc}"
    for value in secrets:
        if len(value) >= 4:  # noqa: PLR2004
            message = message.replace(value, "****")
    return message[:MAX_ERROR_LENGTH]


def chunk_records(
    records: Sequence[UsageRecord], size: int = MAX_RECORDS_PER_MESSAGE
) -> list[dict[str, Any]]:
    """Empaqueta registros push en mensajes SQS (< 256 KiB cada uno)."""
    return [
        RecordsTask(records=list(records[i : i + size])).model_dump(mode="json")
        for i in range(0, len(records), size)
    ]
