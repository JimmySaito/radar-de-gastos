"""Composition root: construye el grafo de dependencias según el entorno.

Las instancias se crean una vez por contenedor de Lambda (fuera del handler), de modo
que las conexiones y la caché de secretos se reutilizan entre invocaciones *warm*.
``STORAGE=memory`` ejecuta todo en memoria (pruebas); ``STORAGE=local`` además persiste
configuración y credenciales en ``~/.radar-gastos`` para usar la aplicación sin AWS.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import cache

from radar_gastos.domain.ports import (
    AlertPublisher,
    ConfigRepository,
    MappingRepository,
    RecurringRepository,
    SecretStore,
    UsageRepository,
    WorkQueue,
)
from radar_gastos.services.budgets import AlertLedger


@dataclass(frozen=True, slots=True)
class Settings:
    storage: str
    table_name: str
    queue_url: str
    alert_topic_arn: str
    auth_mode: str
    m2m_client_tenants: dict[str, str]
    demo_tenant: str
    aggregate_shards: int = 1
    read_cache_seconds: float = 0.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            storage=os.environ.get("STORAGE", "dynamodb"),
            table_name=os.environ.get("TABLE_NAME", "radar-gastos"),
            queue_url=os.environ.get("QUEUE_URL", ""),
            alert_topic_arn=os.environ.get("ALERT_TOPIC_ARN", ""),
            auth_mode=os.environ.get("AUTH_MODE", "jwt"),
            m2m_client_tenants=json.loads(os.environ.get("M2M_CLIENT_TENANTS", "{}")),
            demo_tenant=os.environ.get("DEMO_TENANT", "demo"),
            aggregate_shards=int(os.environ.get("AGGREGATE_SHARDS", "1")),
            read_cache_seconds=float(os.environ.get("READ_CACHE_SECONDS", "0")),
        )


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    usage: UsageRepository
    mappings: MappingRepository
    configs: ConfigRepository
    alert_ledger: AlertLedger
    secrets: SecretStore
    queue: WorkQueue
    alerts: AlertPublisher
    recurring: RecurringRepository


def today_utc() -> date:
    return datetime.now(tz=UTC).date()


_retry_listeners: list[Callable[[str], None]] = []


def on_ledger_retry(listener: Callable[[str], None]) -> None:
    """Permite a un handler observar reintentos optimistas (p. ej. publicar métricas)."""
    _retry_listeners.append(listener)


def _notify_retry(reason: str) -> None:
    for listener in _retry_listeners:
        listener(reason)


def _aws_container(settings: Settings) -> Container:
    import boto3
    from botocore.config import Config

    from radar_gastos.adapters.aws import SecretsManagerProvider, SnsAlertPublisher, SqsWorkQueue
    from radar_gastos.adapters.dynamodb import (
        DynamoConfigRepository,
        DynamoMappingRepository,
        DynamoRecurringRepository,
        DynamoUsageRepository,
    )

    cfg = Config(retries={"mode": "adaptive", "max_attempts": 5}, connect_timeout=2, read_timeout=5)
    ddb = boto3.client("dynamodb", config=cfg)
    configs = DynamoConfigRepository(ddb, settings.table_name)
    return Container(
        settings=settings,
        usage=_with_read_cache(
            DynamoUsageRepository(
                ddb,
                settings.table_name,
                on_retry=_notify_retry,
                aggregate_shards=settings.aggregate_shards,
            ),
            settings,
        ),
        mappings=DynamoMappingRepository(ddb, settings.table_name),
        configs=configs,
        alert_ledger=configs,
        secrets=SecretsManagerProvider(boto3.client("secretsmanager", config=cfg)),
        queue=SqsWorkQueue(boto3.client("sqs", config=cfg), settings.queue_url),
        alerts=SnsAlertPublisher(boto3.client("sns", config=cfg), settings.alert_topic_arn),
        recurring=DynamoRecurringRepository(ddb, settings.table_name),
    )


def _memory_container(settings: Settings) -> Container:
    from radar_gastos.adapters.localfs import (
        FileConfigRepository,
        FileMappingRepository,
        FileRecurringRepository,
        FileSecretStore,
        default_home,
    )
    from radar_gastos.adapters.memory import (
        InMemoryAlerts,
        InMemoryConfigRepository,
        InMemoryMappingRepository,
        InMemoryQueue,
        InMemoryRecurringRepository,
        InMemoryUsageRepository,
        StaticSecrets,
    )

    configs: InMemoryConfigRepository
    mappings: InMemoryMappingRepository
    secrets: StaticSecrets
    recurring: InMemoryRecurringRepository
    if settings.storage == "local":
        home = default_home()
        configs, mappings, secrets, recurring = (
            FileConfigRepository(home),
            FileMappingRepository(home),
            FileSecretStore(home),
            FileRecurringRepository(home),
        )
    else:
        configs, mappings, secrets, recurring = (
            InMemoryConfigRepository(),
            InMemoryMappingRepository(),
            StaticSecrets(),
            InMemoryRecurringRepository(),
        )
    return Container(
        settings=settings,
        usage=InMemoryUsageRepository(),
        mappings=mappings,
        configs=configs,
        alert_ledger=configs,
        secrets=secrets,
        queue=InMemoryQueue(),
        alerts=InMemoryAlerts(),
        recurring=recurring,
    )


def _with_read_cache(repo: UsageRepository, settings: Settings) -> UsageRepository:
    """La API activa una caché corta de lecturas (READ_CACHE_SECONDS); el colector no la usa."""
    if settings.read_cache_seconds <= 0:
        return repo
    from radar_gastos.adapters.cache import CachedUsageRepository

    return CachedUsageRepository(repo, settings.read_cache_seconds)


@cache
def container() -> Container:
    """Única instancia por contenedor de ejecución (singleton perezoso)."""
    settings = Settings.from_env()
    if settings.storage in {"memory", "local"}:
        return _memory_container(settings)
    return _aws_container(settings)
