"""API HTTP (API Gateway HTTP API + autorizador JWT de Cognito).

Rutas
-----
GET    /v1/health                          sin auth (health check)
GET    /v1/me                              tenant, permisos y tenant de ejemplo
GET    /v1/summary?from&to[&dataset=demo]  KPIs del dashboard
GET    /v1/timeseries?from&to&group_by     serie diaria (provider|member|team|service)
GET    /v1/identities?from&to              identidades vistas y si están asignadas
POST   /v1/usage/records                   ingesta push (scope radar-gastos/ingest) → 202
GET    /v1/providers                       catálogo de proveedores y campos requeridos
GET    /v1/connectors                      conexiones + estado (sin secretos)        [admins]
PUT    /v1/connectors                      crear/editar conexión                     [admins]
POST   /v1/connectors/test                 probar credenciales sin guardar           [admins]
POST   /v1/connectors/{id}/sync?days=N     sincronizar ahora → 202                   [admins]
DELETE /v1/connectors/{id}                 eliminar conexión y su secreto            [admins]
GET|PUT|DELETE /v1/mappings                principal → persona/equipo   [escritura: admins]
GET|PUT|DELETE /v1/budgets                 presupuestos mensuales       [escritura: admins]
GET    /v1/recurring/{personal|business}/catalog   servicios comunes y categorías
GET    /v1/recurring/{personal|business}           gastos recurrentes + resumen del mes
PUT    /v1/recurring/{personal|business}           crear/editar (business: admins)
DELETE /v1/recurring/{personal|business}/{id}      eliminar
GET|PUT /v1/personal/profiles · DELETE …/{id}       personas del espacio personal

``dataset=demo`` hace que las lecturas usen el tenant de ejemplo (sólo lectura). Las
escrituras siempre van al tenant del token.
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
    NotFoundError,
    UnauthorizedError,
)
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from radar_gastos.connectors.catalog import CATALOG, InvalidConfigurationError
from radar_gastos.domain.ledger import Dimension
from radar_gastos.domain.models import Budget, Identifier, PrincipalMapping, Provider, UsageRecord
from radar_gastos.domain.recurring import Scope
from radar_gastos.domain.recurring_catalog import catalog as recurring_catalog
from radar_gastos.handlers.bootstrap import container, today_utc
from radar_gastos.services.budgets import BudgetEvaluator
from radar_gastos.services.connections import (
    ConnectionNotFoundError,
    ConnectionRequest,
    ConnectionService,
)
from radar_gastos.services.dashboard import DashboardService, DateRange, InvalidRangeError
from radar_gastos.services.ingestion import chunk_records
from radar_gastos.services.recurring import (
    DEMO_PERSONAL_OWNER,
    ExpenseNotFoundError,
    InvalidExpenseError,
    ProfileRequest,
    RecurringRequest,
    RecurringService,
    owner_key,
)

logger = Logger(service="radar-gastos-api")
tracer = Tracer(service="radar-gastos-api")
metrics = Metrics(namespace="RadarDeGastos", service="api")
app = APIGatewayHttpResolver(cors=CORSConfig(allow_origin="*", max_age=600), strip_prefixes=[])

MAX_INGEST_RECORDS = 1000
INGEST_SCOPE = "radar-gastos/ingest"
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
        local_tenant = str(app.current_event.headers.get("x-tenant-id", "local"))
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


def read_tenant() -> str:
    """Tenant para lecturas: el del token o, con ``dataset=demo``, el de ejemplo."""
    caller = current_caller()
    if app.current_event.get_query_string_value("dataset") == "demo":
        return container().settings.demo_tenant
    return caller.tenant_id


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


def _query(name: str) -> str:
    value = app.current_event.get_query_string_value(name)
    if not value:
        raise BadRequestError(f"falta el parámetro '{name}'")
    return value


def _provider(value: str | None) -> Provider | None:
    if not value or value == "*":
        return None
    try:
        return Provider(value)
    except ValueError as exc:
        raise BadRequestError(f"proveedor desconocido: {value}") from exc


def _connections() -> ConnectionService:
    c = container()
    return ConnectionService(c.configs, c.secrets, c.queue, today_utc)


@app.exception_handler(InvalidConfigurationError)  # type: ignore[untyped-decorator]
def _invalid_config(exc: InvalidConfigurationError) -> Response[Any]:
    return Response(400, "application/json", {"statusCode": 400, "message": str(exc)})


@app.exception_handler(ConnectionNotFoundError)  # type: ignore[untyped-decorator]
def _not_found(exc: ConnectionNotFoundError) -> Response[Any]:
    return Response(404, "application/json", {"statusCode": 404, "message": str(exc)})


@app.exception_handler(InvalidExpenseError)  # type: ignore[untyped-decorator]
def _invalid_expense(exc: InvalidExpenseError) -> Response[Any]:
    return Response(400, "application/json", {"statusCode": 400, "message": str(exc)})


@app.exception_handler(ExpenseNotFoundError)  # type: ignore[untyped-decorator]
def _expense_not_found(exc: ExpenseNotFoundError) -> Response[Any]:
    return Response(404, "application/json", {"statusCode": 404, "message": str(exc)})


# ---------------------------------------------------------------- lectura
@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/me")
def me() -> dict[str, Any]:
    caller = current_caller()
    settings = container().settings
    return {
        "tenant_id": caller.tenant_id,
        "is_admin": caller.is_admin,
        "demo_tenant": settings.demo_tenant,
        "auth_mode": settings.auth_mode,
        "connections": len(container().configs.list_connectors(caller.tenant_id)),
    }


@app.get("/v1/summary")
@tracer.capture_method
def summary() -> dict[str, Any]:
    return DashboardService(container().usage).summary(read_tenant(), _range())


@app.get("/v1/timeseries")
@tracer.capture_method
def timeseries() -> dict[str, Any]:
    tenant = read_tenant()
    group_by = app.current_event.get_query_string_value("group_by", "provider")
    if group_by == "provider":
        dimension = None
    else:
        try:
            dimension = Dimension(group_by)
        except ValueError as exc:
            raise BadRequestError("group_by debe ser provider|member|team|service") from exc
    return DashboardService(container().usage).timeseries(tenant, _range(), dimension)


@app.get("/v1/identities")
def identities() -> dict[str, Any]:
    tenant = read_tenant()
    c = container()
    rows = DashboardService(c.usage).identities(tenant, _range(), c.mappings.list_mappings(tenant))
    return {"items": rows}


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


# ---------------------------------------------------------------- conexiones
@app.get("/v1/providers")
def providers() -> dict[str, Any]:
    current_caller()
    return {"items": [spec.as_dict() for spec in CATALOG.values()]}


@app.get("/v1/connectors")
def list_connectors() -> dict[str, Any]:
    caller = require_admin()
    return {"items": _connections().list(caller.tenant_id)}


@app.put("/v1/connectors")
def put_connector() -> dict[str, Any]:
    caller = require_admin()
    request: ConnectionRequest = _body(ConnectionRequest)
    config = _connections().save(caller.tenant_id, request)
    logger.info("conexión guardada", extra={"by": caller.subject, "connector": config.connector_id})
    return next(
        i for i in _connections().list(caller.tenant_id) if i["connector_id"] == config.connector_id
    )


@app.post("/v1/connectors/test")
def test_connector() -> dict[str, Any]:
    caller = require_admin()
    request: ConnectionRequest = _body(ConnectionRequest)
    return _connections().test(caller.tenant_id, request).as_dict()


@app.post("/v1/connectors/<connector_id>/sync")
def sync_connector(connector_id: str) -> Response[Any]:
    caller = require_admin()
    days_raw = app.current_event.get_query_string_value("days")
    try:
        days = int(days_raw) if days_raw else None
    except ValueError as exc:
        raise BadRequestError("days debe ser un entero") from exc
    task = _connections().sync(caller.tenant_id, connector_id, days)
    return Response(
        202,
        "application/json",
        {"queued": True, "from": task.start.isoformat(), "to": task.end.isoformat()},
    )


@app.delete("/v1/connectors/<connector_id>")
def delete_connector(connector_id: str) -> dict[str, Any]:
    caller = require_admin()
    _connections().delete(caller.tenant_id, connector_id)
    logger.info("conexión eliminada", extra={"by": caller.subject, "connector": connector_id})
    return {"deleted": connector_id}


# ---------------------------------------------------------------- gobierno
@app.get("/v1/mappings")
def list_mappings() -> dict[str, Any]:
    tenant = read_tenant()
    return {
        "items": [m.model_dump(mode="json") for m in container().mappings.list_mappings(tenant)]
    }


@app.put("/v1/mappings")
def put_mapping() -> dict[str, Any]:
    caller = require_admin()
    mapping: PrincipalMapping = _body(PrincipalMapping)
    container().mappings.put_mapping(caller.tenant_id, mapping)
    logger.info("mapeo actualizado", extra={"by": caller.subject, "principal": mapping.principal})
    return mapping.model_dump(mode="json")


@app.delete("/v1/mappings")
def delete_mapping() -> dict[str, Any]:
    caller = require_admin()
    provider = _provider(_query("provider"))
    if provider is None:
        raise BadRequestError("provider es obligatorio")
    principal = _query("principal")
    container().mappings.delete_mapping(caller.tenant_id, provider, principal)
    return {"deleted": principal}


class BudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: Identifier
    provider: Provider | None = None
    monthly_limit: Decimal = Field(gt=0)


@app.get("/v1/budgets")
def list_budgets() -> dict[str, Any]:
    tenant = read_tenant()
    c = container()
    statuses = BudgetEvaluator(c.usage, c.configs, c.alert_ledger, c.alerts).status(
        tenant, today_utc()
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


@app.delete("/v1/budgets")
def delete_budget() -> dict[str, Any]:
    caller = require_admin()
    team = _query("team")
    container().configs.delete_budget(
        caller.tenant_id, team, _provider(app.current_event.get_query_string_value("provider"))
    )
    return {"deleted": team}


# ---------------------------------------------------------------- gastos recurrentes
def _scope(value: str) -> Scope:
    try:
        return Scope(value)
    except ValueError as exc:
        raise NotFoundError("el espacio debe ser personal o business") from exc


def _recurring_owner(scope: Scope, *, write: bool) -> str:
    """Personal: cada persona gestiona lo suyo. Empresa: todos leen, solo admins escriben.

    ``dataset=demo`` en una lectura personal devuelve el hogar de ejemplo (solo lectura)."""
    caller = require_admin() if write and scope is Scope.BUSINESS else current_caller()
    demo = app.current_event.get_query_string_value("dataset") == "demo"
    if demo and not write and scope is Scope.PERSONAL:
        return DEMO_PERSONAL_OWNER
    return owner_key(scope, subject=caller.subject, tenant_id=caller.tenant_id)


def _recurring(owner: str | None = None) -> RecurringService:
    """El ejemplo personal se genera al vuelo (fechas relativas a hoy) en un repositorio en
    memoria: siempre está fresco y nunca se escribe en la base de datos."""
    if owner == DEMO_PERSONAL_OWNER:
        from radar_gastos.adapters.memory import InMemoryRecurringRepository
        from radar_gastos.services.demo import personal_demo

        repo = InMemoryRecurringRepository()
        profiles, expenses = personal_demo(today_utc())
        for profile in profiles:
            repo.put_profile(owner, profile)
        for expense in expenses:
            repo.put_expense(owner, expense)
        return RecurringService(repo, today_utc)
    return RecurringService(container().recurring, today_utc)


@app.get("/v1/recurring/<scope>/catalog")
def get_recurring_catalog(scope: str) -> dict[str, Any]:
    current_caller()
    return recurring_catalog(_scope(scope))


@app.get("/v1/recurring/<scope>")
def list_recurring(scope: str) -> dict[str, Any]:
    owner = _recurring_owner(_scope(scope), write=False)
    member = app.current_event.get_query_string_value("member") or None
    return _recurring(owner).overview(owner, member)


@app.put("/v1/recurring/<scope>")
def put_recurring(scope: str) -> dict[str, Any]:
    space = _scope(scope)
    owner = _recurring_owner(space, write=True)
    request: RecurringRequest = _body(RecurringRequest)
    return _recurring().save(owner, space, request).model_dump(mode="json")


@app.delete("/v1/recurring/<scope>/<expense_id>")
def delete_recurring(scope: str, expense_id: str) -> dict[str, Any]:
    owner = _recurring_owner(_scope(scope), write=True)
    _recurring().delete(owner, expense_id)
    return {"deleted": expense_id}


# ---------------------------------------------------------------- personas (personal)
@app.get("/v1/personal/profiles")
def list_profiles() -> dict[str, Any]:
    owner = _recurring_owner(Scope.PERSONAL, write=False)
    return {"items": _recurring(owner).overview(owner)["profiles"]}


@app.put("/v1/personal/profiles")
def put_profile() -> dict[str, Any]:
    owner = _recurring_owner(Scope.PERSONAL, write=True)
    request: ProfileRequest = _body(ProfileRequest)
    return _recurring().save_profile(owner, request).model_dump(mode="json")


@app.delete("/v1/personal/profiles/<profile_id>")
def delete_profile(profile_id: str) -> dict[str, Any]:
    owner = _recurring_owner(Scope.PERSONAL, write=True)
    updated = _recurring().delete_profile(owner, profile_id)
    return {"deleted": profile_id, "expenses_updated": updated}


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_HTTP)
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
