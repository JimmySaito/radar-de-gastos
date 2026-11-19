"""Casos de uso de configuración de conexiones: alta, prueba, sincronización y baja.

Reglas de seguridad:
- Las credenciales se separan de las opciones según el catálogo y van al ``SecretStore``;
  la configuración sólo guarda la referencia.
- Nunca se devuelven valores secretos: la UI recibe sólo *qué* campos están definidos.
- Los mensajes de error se depuran (``safe_error``) antes de mostrarse.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from radar_gastos.connectors.catalog import InvalidConfigurationError, spec_for, split_values
from radar_gastos.domain.models import ConnectorConfig, Provider
from radar_gastos.domain.ports import CollectionWindow, ConfigRepository, SecretStore, WorkQueue
from radar_gastos.services.ingestion import CollectTask, ConnectorBuilder, safe_error

TEST_WINDOW_DAYS = 3
MAX_SYNC_DAYS = 31
_SLUG = re.compile(r"[^a-z0-9-]+")


class ConnectionRequest(BaseModel):
    """Lo que envía el formulario de la UI."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str | None = Field(default=None, max_length=64)
    provider: Provider
    label: str = Field(default="", max_length=120)
    values: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    lookback_days: int = Field(default=3, ge=1, le=MAX_SYNC_DAYS)


@dataclass(frozen=True, slots=True)
class TestResult:
    ok: bool
    records: int = 0
    cost: Decimal = Decimal(0)
    error: str | None = None
    window_start: date | None = None
    window_end: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "records": self.records,
            "cost": str(self.cost.quantize(Decimal("0.01"))),
            "error": self.error,
            "window": [
                self.window_start.isoformat() if self.window_start else None,
                self.window_end.isoformat() if self.window_end else None,
            ],
        }


class ConnectionNotFoundError(LookupError):
    pass


class ConnectionService:
    def __init__(
        self,
        configs: ConfigRepository,
        secrets: SecretStore,
        queue: WorkQueue,
        today: Callable[[], date],
        connector_builder: ConnectorBuilder | None = None,
    ) -> None:
        from radar_gastos.connectors.registry import build_connector

        self._configs = configs
        self._secrets = secrets
        self._queue = queue
        self._today = today
        self._build = connector_builder or build_connector

    # ------------------------------------------------------------- consulta
    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        statuses = self._configs.list_statuses(tenant_id)
        items = []
        for config in sorted(
            self._configs.list_connectors(tenant_id), key=lambda c: c.connector_id
        ):
            status = statuses.get(config.connector_id)
            saved = self._secrets.get_secret(config.secret_arn) if config.secret_arn else {}
            items.append(
                {
                    "connector_id": config.connector_id,
                    "provider": config.provider.value,
                    "label": config.label or spec_for_safe(config.provider),
                    "enabled": config.enabled,
                    "lookback_days": config.lookback_days,
                    "options": config.options,
                    "secrets_set": sorted(k for k, v in saved.items() if v),
                    "status": status.model_dump(mode="json") if status else None,
                }
            )
        return items

    def get(self, tenant_id: str, connector_id: str) -> ConnectorConfig:
        for config in self._configs.list_connectors(tenant_id):
            if config.connector_id == connector_id:
                return config
        raise ConnectionNotFoundError(f"no existe la conexión {connector_id}")

    # ------------------------------------------------------------- escritura
    def save(self, tenant_id: str, request: ConnectionRequest) -> ConnectorConfig:
        existing = self._existing(tenant_id, request.connector_id)
        if existing and existing.provider is not request.provider:
            raise InvalidConfigurationError("no se puede cambiar el proveedor de una conexión")
        previous_secret = (
            self._secrets.get_secret(existing.secret_arn)
            if existing and existing.secret_arn
            else {}
        )
        secrets, options = split_values(request.provider, request.values, previous_secret)
        connector_id = existing.connector_id if existing else self._new_id(request)

        secret_ref = existing.secret_arn if existing else None
        if secrets and secrets != dict(previous_secret):
            secret_ref = self._secrets.put_secret(tenant_id, connector_id, secrets)
            if existing and existing.secret_arn and existing.secret_arn != secret_ref:
                self._secrets.delete_secret(existing.secret_arn)
        elif not secrets and secret_ref:
            self._secrets.delete_secret(secret_ref)
            secret_ref = None

        config = ConnectorConfig(
            tenant_id=tenant_id,
            connector_id=connector_id,
            provider=request.provider,
            label=request.label.strip(),
            secret_arn=secret_ref,
            enabled=request.enabled,
            lookback_days=request.lookback_days,
            options=options,
        )
        self._configs.put_connector(config)
        return config

    def delete(self, tenant_id: str, connector_id: str) -> None:
        config = self.get(tenant_id, connector_id)
        if config.secret_arn:
            self._secrets.delete_secret(config.secret_arn)
        self._configs.delete_connector(tenant_id, connector_id)

    # ------------------------------------------------------------- operación
    def test(self, tenant_id: str, request: ConnectionRequest) -> TestResult:
        """Lee los últimos días del proveedor sin escribir nada: valida credenciales y permisos."""
        end = self._today() + timedelta(days=1)
        window = CollectionWindow(end - timedelta(days=TEST_WINDOW_DAYS), end)
        secrets: dict[str, str] = {}
        try:
            existing = self._existing(tenant_id, request.connector_id)
            previous = (
                self._secrets.get_secret(existing.secret_arn)
                if existing and existing.secret_arn
                else {}
            )
            secrets, options = split_values(request.provider, request.values, previous)
            config = ConnectorConfig(
                tenant_id=tenant_id,
                connector_id=request.connector_id or "test",
                provider=request.provider,
                options=options,
            )
            records = list(self._build(config, secrets).fetch(tenant_id, window))
        except Exception as exc:
            return TestResult(
                ok=False,
                error=safe_error(exc, list(secrets.values())),
                window_start=window.start,
                window_end=window.end,
            )
        return TestResult(
            ok=True,
            records=len(records),
            cost=sum((r.cost for r in records), Decimal(0)),
            window_start=window.start,
            window_end=window.end,
        )

    def sync(self, tenant_id: str, connector_id: str, days: int | None = None) -> CollectTask:
        config = self.get(tenant_id, connector_id)
        span = max(1, min(days or config.lookback_days, MAX_SYNC_DAYS))
        end = self._today() + timedelta(days=1)
        task = CollectTask(
            tenant_id=tenant_id,
            connector_id=connector_id,
            start=end - timedelta(days=span),
            end=end,
        )
        self._queue.send_batch([task.model_dump(mode="json")])
        return task

    # ------------------------------------------------------------- auxiliares
    def _existing(self, tenant_id: str, connector_id: str | None) -> ConnectorConfig | None:
        if not connector_id:
            return None
        try:
            return self.get(tenant_id, connector_id)
        except ConnectionNotFoundError:
            return None

    @staticmethod
    def _new_id(request: ConnectionRequest) -> str:
        base = _SLUG.sub("-", (request.label or request.provider.value).lower()).strip("-")[:40]
        return f"{base or request.provider.value}-{uuid.uuid4().hex[:6]}"


def spec_for_safe(provider: Provider) -> str:
    try:
        return spec_for(provider).name
    except InvalidConfigurationError:
        return provider.value
