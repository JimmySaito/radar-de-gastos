"""Modelo canónico de consumo (Canonical Data Model).

Cada proveedor expone su consumo con formas distintas (tokens, requests, USD en
centavos, USD en float, horas de cómputo...). Los conectores traducen esas formas
a ``UsageRecord``, el único contrato que conoce el resto del sistema.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
TenantId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

UNATTRIBUTED_MEMBER = "unattributed"
UNASSIGNED_TEAM = "unassigned"


class Provider(StrEnum):
    """Proveedores soportados. ``CUSTOM`` cubre cualquier sistema vía la API de ingesta."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    AWS = "aws"
    CUSTOM = "custom"
    SYNTHETIC = "synthetic"


class UsageRecord(BaseModel):
    """Hecho de consumo diario, normalizado e inmutable.

    La identidad natural es (tenant, provider, service, principal, usage_date). Una
    nueva lectura del mismo día *revisa* el hecho (los proveedores consolidan datos
    hasta 24 h después), por lo que el almacenamiento hace upsert y no acumula.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    provider: Provider
    service: Identifier = Field(
        description="Modelo de IA o servicio de nube (p. ej. claude-opus-5-5, AmazonEC2)"
    )
    principal: Identifier = Field(
        description="Identidad técnica del proveedor: API key, usuario, cuenta o tag"
    )
    usage_date: date
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)
    currency: CurrencyCode = "USD"

    @model_validator(mode="after")
    def _has_signal(self) -> Self:
        if not any(
            (self.input_tokens, self.output_tokens, self.cached_tokens, self.requests, self.cost)
        ):
            raise ValueError("un UsageRecord debe tener al menos una métrica distinta de cero")
        return self

    @property
    def natural_key(self) -> str:
        return "|".join(
            (
                self.tenant_id,
                self.provider.value,
                self.service,
                self.principal,
                self.usage_date.isoformat(),
            )
        )

    @property
    def record_id(self) -> str:
        """Identificador determinista: base de la idempotencia en todo el pipeline."""
        return hashlib.sha256(self.natural_key.encode()).hexdigest()[:32]

    @property
    def content_hash(self) -> str:
        """Huella de las métricas; si no cambia, la reescritura es un no-op."""
        parts = (
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.requests,
            self.cost.normalize(),
            self.currency,
        )
        payload = "|".join(str(p) for p in parts)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cached_tokens


class Metrics(BaseModel):
    """Vector de métricas aditivas, usado para agregados y deltas."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    requests: int = 0
    cost: Decimal = Decimal(0)

    @classmethod
    def of(cls, record: UsageRecord | None) -> Metrics:
        if record is None:
            return cls()
        return cls(
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cached_tokens=record.cached_tokens,
            requests=record.requests,
            cost=record.cost,
        )

    def __add__(self, other: Metrics) -> Metrics:
        return Metrics(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            requests=self.requests + other.requests,
            cost=self.cost + other.cost,
        )

    def __neg__(self) -> Metrics:
        return Metrics(
            input_tokens=-self.input_tokens,
            output_tokens=-self.output_tokens,
            cached_tokens=-self.cached_tokens,
            requests=-self.requests,
            cost=-self.cost,
        )

    def __sub__(self, other: Metrics) -> Metrics:
        return self + (-other)

    @property
    def is_zero(self) -> bool:
        return not any(
            (self.input_tokens, self.output_tokens, self.cached_tokens, self.requests, self.cost)
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cached_tokens


class Attribution(BaseModel):
    """Resultado de resolver un principal técnico a una persona y un equipo (showback)."""

    model_config = ConfigDict(frozen=True)

    member: str = UNATTRIBUTED_MEMBER
    team: str = UNASSIGNED_TEAM


class PrincipalMapping(BaseModel):
    """Regla de gobierno: qué persona/equipo es dueño de una identidad técnica."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    principal: Identifier
    member: Identifier
    team: Identifier


class ConnectorConfig(BaseModel):
    """Configuración de un conector para un tenant. Las credenciales viven en Secrets Manager."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    connector_id: Identifier
    provider: Provider
    label: str = Field(default="", max_length=120)
    secret_arn: str | None = None
    enabled: bool = True
    lookback_days: int = Field(default=3, ge=1, le=31)
    options: dict[str, str] = Field(default_factory=dict)


class Budget(BaseModel):
    """Presupuesto mensual por equipo (y opcionalmente por proveedor)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    team: Identifier
    provider: Provider | None = None
    monthly_limit: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: CurrencyCode = "USD"
    alert_thresholds: tuple[Decimal, ...] = (Decimal("0.5"), Decimal("0.8"), Decimal("1.0"))


class ConnectorStatus(BaseModel):
    """Resultado de la última sincronización de un conector (soportabilidad y UX)."""

    model_config = ConfigDict(frozen=True)

    last_sync_at: datetime
    ok: bool
    written: int = 0
    unchanged: int = 0
    error: str | None = None
    window_start: date | None = None
    window_end: date | None = None
