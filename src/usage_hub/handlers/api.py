"""API HTTP (API Gateway HTTP API + autorizador JWT de Cognito).

Rutas
-----
GET  /v1/health                       sin auth (health check)
GET  /v1/summary?from&to              KPIs del dashboard
GET  /v1/timeseries?from&to&group_by  serie diaria (provider|member|team|service)
POST /v1/usage/records                ingesta push (scope usagehub/ingest) → 202
GET  /v1/mappings | PUT /v1/mappings  gobierno: principal → persona/equipo (admins)
GET  /v1/connectors | PUT /v1/connectors  configuración de conectores (admins)
GET  /v1/budgets | PUT /v1/budgets    presupuestos y su estado (admins para escribir)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, CORSConfig, Response
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    ForbiddenError,
    UnauthorizedError,
)
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from usage_hub.domain.ledger import Dimension
from usage_hub.domain.models import (
    Budget,
    ConnectorConfig,
    Identifier,
    PrincipalMapping,
    Provider,
    UsageRecord,
)
from usage_hub.handlers.bootstrap import container, today_utc
from usage_hub.services.budgets import BudgetEvaluator
from usage_hub.services.dashboard import DashboardService, DateRange, InvalidRangeError
from usage_hub.services.ingestion import chunk_records

logger = Logger(service="usagehub-api")
tracer = Tracer(service="usagehub-api")
metrics = Metrics(namespace="UsageHub", service="api")
app = APIGatewayHttpResolver(cors=CORSConfig(allow_origin="*", max_age=600), strip_prefixes=[])

MAX_INGEST_RECORDS = 1000
INGEST_SCOPE = "usagehub/ingest"
ADMIN_GROUP = "admins"


# ---------------------------------------------------------------- seguridad
class Caller(BaseModel):
    tenant_id: str
    subject: str
    is_admin: bool
    can_ingest: bool


def current_caller() -> Caller:
    """Deriva tenant y permisos de las *claims* del JWT ya validado por API Gateway.

    El tenant nunca se toma del cuerpo de la petición: evita que un cliente escriba
    o lea datos de otro tenant (aislamiento multi-tenant, OWASP API1 - BOLA).
    """
    settings = container().settings
    if settings.auth_mode == "local":
        local_tenant = str(app.current_event.headers.get("x-tenant-id", "demo"))
        return Caller(tenant_id=local_tenant, subject="local", is_admin=True, can_ingest=True)

    claims: dict[str, Any] = (
        app.current_event.raw_event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    if not claims:
        raise UnauthorizedError("token ausente")
    scopes = set(str(claims.get("scope", "")).split())
    groups = str(claims.get("cognito:groups", "")).strip("[]").replace(",", " ").split()
    tenant: object = claims.get("custom:tenant_id") or settings.m2m_client_tenants.get(
        str(claims.get("client_id", ""))
    )
    if not tenant:
        raise ForbiddenError("el token no está asociado a un tenant")
    return Caller(
        tenant_id=str(tenant),
        subject=str(claims.get("sub", claims.get("client_id", "unknown"))),
        is_admin=ADMIN_GROUP in groups,
        can_ingest=INGEST_SCOPE in scopes or ADMIN_GROUP in groups,
    )


def require_admin() -> Caller:
    caller = current_caller()
    if not caller.is_admin:
        raise ForbiddenError("se requiere el grupo 'admins'")
    return caller


def _body(model: type[BaseModel]) -> Any:
    try:
        return model.model_validate(app.current_event.json_body or {})
    except ValidationError as exc:
        raise BadRequestError(exc.json(include_url=False)) from exc


def _range() -> DateRange:
    try:
        return DateRange.parse(
            app.current_event.get_query_string_value("from"),
            app.current_event.get_query_string_value("to"),
            today_utc(),
        )
    except InvalidRangeError as exc:
        raise BadRequestError(str(exc)) from exc


# ---------------------------------------------------------------- lectura
@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/summary")
@tracer.capture_method
def summary() -> dict[str, Any]:
    caller = current_caller()
    return DashboardService(container().usage).summary(caller.tenant_id, _range())


@app.get("/v1/timeseries")
@tracer.capture_method
def timeseries() -> dict[str, Any]:
    caller = current_caller()
    group_by = app.current_event.get_query_string_value("group_by", "provider")
    if group_by == "provider":
        dimension = None
    else:
        try:
            dimension = Dimension(group_by)
        except ValueError as exc:
            raise BadRequestError("group_by debe ser provider|member|team|service") from exc
    return DashboardService(container().usage).timeseries(caller.tenant_id, _range(), dimension)


# ---------------------------------------------------------------- ingesta push
class IngestRecord(BaseModel):
    """Registro de consumo enviado por un sistema externo (sin tenant: lo pone el token)."""

    model_config = ConfigDict(extra="forbid")

    provider: Provider = Provider.CUSTOM
    service: Identifier
    principal: Identifier
    usage_date: date
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal(0), ge=0)
    currency: str = "USD"


class IngestRequest(BaseModel):
    records: list[IngestRecord] = Field(min_length=1, max_length=MAX_INGEST_RECORDS)


@app.post("/v1/usage/records")
@tracer.capture_method
def ingest_records() -> Response[Any]:
    caller = current_caller()
    if not caller.can_ingest:
        raise ForbiddenError(f"se requiere el scope {INGEST_SCOPE}")
    request: IngestRequest = _body(IngestRequest)
    try:
        records = [
            UsageRecord(tenant_id=caller.tenant_id, **r.model_dump()) for r in request.records
        ]
    except ValidationError as exc:
        raise BadRequestError(exc.json(include_url=False)) from exc
    container().queue.send_batch(chunk_records(records))
    metrics.add_metric(name="RecordsAccepted", unit=MetricUnit.Count, value=len(records))
    return Response(
        status_code=202,
        content_type="application/json",
        body={"accepted": len(records), "record_ids": [r.record_id for r in records]},
    )


# ---------------------------------------------------------------- gobierno
@app.get("/v1/mappings")
def list_mappings() -> dict[str, Any]:
    caller = current_caller()
    return {
        "items": [
            m.model_dump(mode="json") for m in container().mappings.list_mappings(caller.tenant_id)
        ]
    }


@app.put("/v1/mappings")
def put_mapping() -> dict[str, Any]:
    caller = require_admin()
    mapping: PrincipalMapping = _body(PrincipalMapping)
    container().mappings.put_mapping(caller.tenant_id, mapping)
    logger.info("mapeo actualizado", extra={"by": caller.subject, "principal": mapping.principal})
    return mapping.model_dump(mode="json")


class ConnectorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: Identifier
    provider: Provider
    secret_arn: str | None = None
    enabled: bool = True
    lookback_days: int = Field(default=3, ge=1, le=31)
    options: dict[str, str] = Field(default_factory=dict)


@app.get("/v1/connectors")
def list_connectors() -> dict[str, Any]:
    caller = require_admin()
    return {
        "items": [
            c.model_dump(mode="json") for c in container().configs.list_connectors(caller.tenant_id)
        ]
    }


@app.put("/v1/connectors")
def put_connector() -> dict[str, Any]:
    caller = require_admin()
    body: ConnectorRequest = _body(ConnectorRequest)
    config = ConnectorConfig(tenant_id=caller.tenant_id, **body.model_dump())
    container().configs.put_connector(config)
    return config.model_dump(mode="json")


class BudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: Identifier
    provider: Provider | None = None
    monthly_limit: Decimal = Field(gt=0)


@app.get("/v1/budgets")
def list_budgets() -> dict[str, Any]:
    caller = current_caller()
    c = container()
    statuses = BudgetEvaluator(c.usage, c.configs, c.alert_ledger, c.alerts).status(
        caller.tenant_id, today_utc()
    )
    return {
        "items": [
            {
                **s.budget.model_dump(mode="json"),
                "spent": str(s.spent.quantize(Decimal("0.01"))),
                "ratio": float(round(s.ratio, 4)),
            }
            for s in statuses
        ]
    }


@app.put("/v1/budgets")
def put_budget() -> dict[str, Any]:
    caller = require_admin()
    body: BudgetRequest = _body(BudgetRequest)
    budget = Budget(tenant_id=caller.tenant_id, **body.model_dump())
    container().configs.put_budget(budget)
    return budget.model_dump(mode="json")


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_HTTP)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
