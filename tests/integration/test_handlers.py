"""Pruebas de los handlers de Lambda con eventos reales de API Gateway v2 y SQS."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from radar_gastos.adapters.memory import InMemoryQueue
from radar_gastos.domain.models import ConnectorConfig, Provider
from radar_gastos.handlers import bootstrap

pytestmark = pytest.mark.integration


class Ctx:
    function_name = "test"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:test"
    aws_request_id = "req-1"


@pytest.fixture
def memory_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[bootstrap.Container]:
    monkeypatch.setenv("STORAGE", "memory")
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("M2M_CLIENT_TENANTS", json.dumps({"m2m-client": "acme"}))
    bootstrap.container.cache_clear()
    yield bootstrap.container()
    bootstrap.container.cache_clear()


def http_event(
    method: str,
    path: str,
    *,
    claims: dict[str, Any] | None = None,
    body: Any = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "http": {"method": method, "path": path, "sourceIp": "1.1.1.1"},
        "requestId": "r",
        "stage": "$default",
    }
    if claims is not None:
        ctx["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "queryStringParameters": query,
        "headers": {"content-type": "application/json"},
        "requestContext": ctx,
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


USER = {"sub": "u1", "custom:tenant_id": "acme", "cognito:groups": "[admins]"}
VIEWER = {"sub": "u2", "custom:tenant_id": "acme"}
M2M = {"client_id": "m2m-client", "scope": "radar-gastos/ingest"}


def call(event: dict[str, Any]) -> tuple[int, Any]:
    from radar_gastos.handlers import api

    result = api.handler(event, Ctx())  # type: ignore[arg-type]
    return result["statusCode"], json.loads(result["body"]) if result.get("body") else None


def drain(c: bootstrap.Container) -> dict[str, Any]:
    """Entrega los mensajes encolados al handler colector como un evento SQS."""
    from radar_gastos.handlers import collector

    assert isinstance(c.queue, InMemoryQueue)
    records = [
        {
            "messageId": str(i),
            "receiptHandle": "h",
            "body": json.dumps(m, default=str),
            "attributes": {
                "SentTimestamp": str(int(time.time() * 1000)),
                "ApproximateReceiveCount": "1",
            },
            "messageAttributes": {},
            "md5OfBody": "",
            "eventSource": "aws:sqs",
            "eventSourceARN": "arn:aws:sqs:us-east-1:0:work",
            "awsRegion": "us-east-1",
        }
        for i, m in enumerate(c.queue.messages)
    ]
    c.queue.messages.clear()
    return collector.handler({"Records": records}, Ctx())  # type: ignore[arg-type]


def test_health_needs_no_auth(memory_env: bootstrap.Container) -> None:
    assert call(http_event("GET", "/v1/health")) == (200, {"status": "ok"})


def test_requests_without_claims_are_rejected(memory_env: bootstrap.Container) -> None:
    assert call(http_event("GET", "/v1/summary"))[0] == 401


def test_push_ingest_end_to_end(memory_env: bootstrap.Container) -> None:
    today = bootstrap.today_utc().isoformat()
    body = {
        "records": [
            {
                "service": "internal-llm",
                "principal": "svc:search",
                "usage_date": today,
                "input_tokens": 100,
                "cost": "0.5",
            },
            {
                "provider": "openai",
                "service": "gpt",
                "principal": "user:bob",
                "usage_date": today,
                "requests": 2,
                "cost": "1.5",
                "tenant_id": "evil",
            },
        ]
    }
    status, payload = call(http_event("POST", "/v1/usage/records", claims=M2M, body=body))
    assert status == 400  # campo tenant_id prohibido: el tenant lo define el token

    body["records"][1].pop("tenant_id")
    status, payload = call(http_event("POST", "/v1/usage/records", claims=M2M, body=body))
    assert status == 202 and payload["accepted"] == 2

    result = drain(memory_env)
    assert result["batchItemFailures"] == []

    status, summary = call(http_event("GET", "/v1/summary", claims=VIEWER))
    assert status == 200
    assert summary["total"]["cost"] == "2.00"
    assert set(summary["by_provider"]) == {"custom", "openai"}

    status, ts = call(
        http_event("GET", "/v1/timeseries", claims=VIEWER, query={"group_by": "service"})
    )
    assert status == 200 and ts["group_by"] == "service"


def test_tenant_isolation(memory_env: bootstrap.Container) -> None:
    other = {"sub": "x", "custom:tenant_id": "globex"}
    today = bootstrap.today_utc().isoformat()
    call(
        http_event(
            "POST",
            "/v1/usage/records",
            claims=M2M,
            body={
                "records": [{"service": "s", "principal": "p", "usage_date": today, "cost": "9"}]
            },
        )
    )
    drain(memory_env)
    _, summary = call(http_event("GET", "/v1/summary", claims=other))
    assert summary["total"]["cost"] == "0.00"


def test_authorization_rules(memory_env: bootstrap.Container) -> None:
    mapping = {
        "provider": "openai",
        "principal": "user:bob",
        "member": "bob@acme.com",
        "team": "data",
    }
    assert call(http_event("PUT", "/v1/mappings", claims=VIEWER, body=mapping))[0] == 403
    assert call(http_event("PUT", "/v1/mappings", claims=USER, body=mapping))[0] == 200
    assert call(http_event("GET", "/v1/mappings", claims=VIEWER))[1]["items"][0]["team"] == "data"
    assert (
        call(http_event("POST", "/v1/usage/records", claims=VIEWER, body={"records": []}))[0] == 403
    )
    assert call(http_event("GET", "/v1/summary", claims={"sub": "no-tenant"}))[0] == 403


def test_connection_lifecycle_via_api(memory_env: bootstrap.Container) -> None:
    """Flujo de la pantalla de Configuración: catálogo → probar → guardar → sync → borrar."""
    status, catalog = call(http_event("GET", "/v1/providers", claims=VIEWER))
    assert status == 200
    assert {p["provider"] for p in catalog["items"]} >= {"anthropic", "openai", "aws", "google"}

    body = {"provider": "synthetic", "label": "Prueba", "values": {"members": "2"}}
    status, result = call(http_event("POST", "/v1/connectors/test", claims=USER, body=body))
    assert status == 200 and result["ok"] and result["records"] > 0

    assert call(http_event("PUT", "/v1/connectors", claims=VIEWER, body=body))[0] == 403
    status, saved = call(http_event("PUT", "/v1/connectors", claims=USER, body=body))
    assert status == 200 and saved["label"] == "Prueba" and saved["status"] is None
    cid = saved["connector_id"]

    status, queued = call(
        http_event("POST", f"/v1/connectors/{cid}/sync", claims=USER, query={"days": "5"})
    )
    assert status == 202 and queued["queued"]
    drain(memory_env)
    items = call(http_event("GET", "/v1/connectors", claims=USER))[1]["items"]
    assert items[0]["status"]["ok"] is True and items[0]["status"]["written"] > 0
    assert call(http_event("GET", "/v1/me", claims=USER))[1]["connections"] == 1

    assert call(http_event("POST", "/v1/connectors/nope/sync", claims=USER))[0] == 404
    assert (
        call(http_event("POST", f"/v1/connectors/{cid}/sync", claims=USER, query={"days": "x"}))[0]
        == 400
    )
    assert call(http_event("DELETE", f"/v1/connectors/{cid}", claims=USER))[0] == 200
    assert call(http_event("GET", "/v1/connectors", claims=USER))[1]["items"] == []


def test_connection_validation_errors(memory_env: bootstrap.Container) -> None:
    missing_key = {"provider": "anthropic", "values": {}}
    status, payload = call(http_event("PUT", "/v1/connectors", claims=USER, body=missing_key))
    assert status == 400 and "alcance de organización" in payload["message"]
    unknown = {"provider": "openai", "values": {"admin_api_key": "k", "hack": "1"}}
    assert call(http_event("PUT", "/v1/connectors", claims=USER, body=unknown))[0] == 400
    status, result = call(http_event("POST", "/v1/connectors/test", claims=USER, body=missing_key))
    assert status == 200 and result["ok"] is False


def test_budgets_and_mappings_admin(memory_env: bootstrap.Container) -> None:
    budget = {"team": "data", "monthly_limit": "100"}
    assert call(http_event("PUT", "/v1/budgets", claims=USER, body=budget))[0] == 200
    items = call(http_event("GET", "/v1/budgets", claims=VIEWER))[1]["items"]
    assert items[0]["spent"] == "0.00"
    bad = {"team": "data", "monthly_limit": "-1"}
    assert call(http_event("PUT", "/v1/budgets", claims=USER, body=bad))[0] == 400
    assert call(http_event("DELETE", "/v1/budgets", claims=USER, query={"team": "data"}))[0] == 200
    assert call(http_event("GET", "/v1/budgets", claims=VIEWER))[1]["items"] == []
    assert call(http_event("DELETE", "/v1/budgets", claims=USER))[0] == 400

    mapping = {"provider": "openai", "principal": "user:x", "member": "x@acme.com", "team": "t"}
    call(http_event("PUT", "/v1/mappings", claims=USER, body=mapping))
    query = {"provider": "openai", "principal": "user:x"}
    assert call(http_event("DELETE", "/v1/mappings", claims=USER, query=query))[0] == 200
    assert call(http_event("GET", "/v1/mappings", claims=USER))[1]["items"] == []
    assert (
        call(
            http_event(
                "DELETE", "/v1/mappings", claims=USER, query={"provider": "nope", "principal": "p"}
            )
        )[0]
        == 400
    )


def test_demo_dataset_is_readable_but_writes_go_to_own_tenant(
    memory_env: bootstrap.Container,
) -> None:
    from radar_gastos.services.demo import seed_demo

    seed_demo(
        memory_env.usage,
        memory_env.mappings,
        memory_env.configs,
        bootstrap.today_utc(),
        days=5,
        members=3,
    )
    own = call(http_event("GET", "/v1/summary", claims=VIEWER))[1]
    demo = call(http_event("GET", "/v1/summary", claims=VIEWER, query={"dataset": "demo"}))[1]
    assert own["total"]["cost"] == "0.00"
    assert demo["tenant_id"] == "demo" and demo["total"]["cost"] != "0.00"
    assert set(demo["by_provider"]) == {"anthropic", "openai", "google", "aws"}
    ids = call(http_event("GET", "/v1/identities", claims=VIEWER, query={"dataset": "demo"}))[1]
    assert ids["items"] and all(i["mapped"] for i in ids["items"])
    budgets = call(http_event("GET", "/v1/budgets", claims=VIEWER, query={"dataset": "demo"}))[1]
    assert len(budgets["items"]) == 4
    # Una escritura con dataset=demo sigue yendo al tenant del token.
    call(
        http_event(
            "PUT",
            "/v1/budgets",
            claims=USER,
            query={"dataset": "demo"},
            body={"team": "x", "monthly_limit": "5"},
        )
    )
    assert len(memory_env.configs.list_budgets("acme")) == 1
    assert len(memory_env.configs.list_budgets("demo")) == 4


@pytest.mark.parametrize("query", [{"from": "2026-13-01"}, {"group_by": "planet"}])
def test_bad_queries(memory_env: bootstrap.Container, query: dict[str, str]) -> None:
    assert call(http_event("GET", "/v1/timeseries", claims=VIEWER, query=query))[0] == 400


def test_scheduler_collector_and_budget_handlers(memory_env: bootstrap.Container) -> None:
    from radar_gastos.handlers import budget, scheduler

    memory_env.configs.put_connector(
        ConnectorConfig(
            tenant_id="acme",
            connector_id="syn",
            provider=Provider.SYNTHETIC,
            options={"members": "2"},
        )
    )
    assert scheduler.handler({}, Ctx()) == {"planned": 1}  # type: ignore[arg-type]
    assert drain(memory_env)["batchItemFailures"] == []

    rng_end = bootstrap.today_utc() + timedelta(days=1)
    from radar_gastos.domain.ledger import Dimension

    written = memory_env.usage.query_aggregates(
        "acme", rng_end - timedelta(days=3), rng_end, Dimension.SERVICE
    )
    assert written  # el conector sintético escribió datos

    result = budget.handler({"tenants": ["acme"]}, Ctx())  # type: ignore[arg-type]
    assert result["tenants"] == 1


def test_collector_reports_partial_failures(memory_env: bootstrap.Container) -> None:
    assert isinstance(memory_env.queue, InMemoryQueue)
    valid = {
        "kind": "records",
        "records": [
            {
                "tenant_id": "acme",
                "provider": "custom",
                "service": "s",
                "principal": "p",
                "usage_date": "2026-11-01",
                "cost": "1",
            }
        ],
    }
    memory_env.queue.messages.extend(
        [
            {
                "kind": "collect",
                "tenant_id": "acme",
                "connector_id": "missing",
                "start": "2026-11-01",
                "end": "2026-11-02",
            },
            valid,
            {"kind": "garbage"},
        ]
    )
    result = drain(memory_env)
    # Sólo los mensajes fallidos vuelven a la cola; el válido se confirma.
    assert [f["itemIdentifier"] for f in result["batchItemFailures"]] == ["0", "2"]


def test_personal_subscriptions_are_private_per_person(memory_env: bootstrap.Container) -> None:
    ana = {"sub": "ana", "custom:tenant_id": "acme"}
    luis = {"sub": "luis", "custom:tenant_id": "acme"}
    body = {
        "name": "Netflix",
        "service": "netflix",
        "category": "streaming",
        "amount": "44900",
        "currency": "COP",
        "billing_date": "2026-11-21",
    }
    status, saved = call(http_event("PUT", "/v1/recurring/personal", claims=ana, body=body))
    assert status == 200 and saved["seats"] == 1
    assert len(call(http_event("GET", "/v1/recurring/personal", claims=ana))[1]["items"]) == 1
    assert call(http_event("GET", "/v1/recurring/personal", claims=luis))[1]["items"] == []
    # Luis no puede borrar lo de Ana: para él ese id no existe.
    assert (
        call(http_event("DELETE", f"/v1/recurring/personal/{saved['expense_id']}", claims=luis))[0]
        == 404
    )
    assert (
        call(http_event("DELETE", f"/v1/recurring/personal/{saved['expense_id']}", claims=ana))[0]
        == 200
    )


def test_business_licenses_admin_writes_everyone_reads(memory_env: bootstrap.Container) -> None:
    body = {
        "name": "Slack",
        "service": "slack",
        "category": "colaboracion",
        "amount": "8.75",
        "billing_date": "2026-11-25",
        "seats": 12,
    }
    assert call(http_event("PUT", "/v1/recurring/business", claims=VIEWER, body=body))[0] == 403
    assert call(http_event("PUT", "/v1/recurring/business", claims=USER, body=body))[0] == 200
    data = call(http_event("GET", "/v1/recurring/business", claims=VIEWER))[1]
    assert data["summary"]["monthly_equivalent"] == {"USD": "105.00"}
    status, error = call(
        http_event(
            "PUT", "/v1/recurring/business", claims=USER, body={**body, "category": "streaming"}
        )
    )
    assert status == 400 and "categoría" in error["message"]
    assert call(http_event("GET", "/v1/recurring/otro", claims=USER))[0] == 404
    cat = call(http_event("GET", "/v1/recurring/personal/catalog", claims=VIEWER))[1]
    assert any(s["key"] == "netflix" for s in cat["services"])


def test_personal_profiles_and_member_filter_via_api(memory_env: bootstrap.Container) -> None:
    ana = {"sub": "ana", "custom:tenant_id": "acme"}
    status, me = call(http_event("PUT", "/v1/personal/profiles", claims=ana, body={"name": "Ana"}))
    assert status == 200
    kid_body = {"name": "Leo", "relation": "Hijo"}
    kid = call(http_event("PUT", "/v1/personal/profiles", claims=ana, body=kid_body))[1]
    body = {
        "name": "Netflix",
        "service": "netflix",
        "category": "streaming",
        "amount": "40000",
        "currency": "COP",
        "billing_date": "2026-11-21",
        "member_ids": [me["profile_id"], kid["profile_id"]],
    }
    assert call(http_event("PUT", "/v1/recurring/personal", claims=ana, body=body))[0] == 200
    query = {"member": kid["profile_id"]}
    data = call(http_event("GET", "/v1/recurring/personal", claims=ana, query=query))[1]
    assert data["summary"]["monthly_equivalent"] == {"COP": "20000.00"}
    assert data["items"][0]["member_names"] == ["Ana", "Leo"]
    assert len(call(http_event("GET", "/v1/personal/profiles", claims=ana))[1]["items"]) == 2
    missing = call(
        http_event("GET", "/v1/recurring/personal", claims=ana, query={"member": "nadie"})
    )
    assert missing[0] == 404
    path = "/v1/personal/profiles/" + kid["profile_id"]
    status, deleted = call(http_event("DELETE", path, claims=ana))
    assert status == 200 and deleted["expenses_updated"] == 1
    assert call(http_event("GET", "/v1/personal/profiles", claims=VIEWER))[1]["items"] == []


def test_personal_demo_is_read_only(memory_env: bootstrap.Container) -> None:
    demo_q = {"dataset": "demo"}
    demo = call(http_event("GET", "/v1/recurring/personal", claims=VIEWER, query=demo_q))[1]
    assert {p["name"] for p in demo["profiles"]} == {"Jimmy", "María", "Sofía"}
    assert demo["summary"]["next_charge"]["days_left"] >= 0
    assert demo["summary"]["upcoming"]
    people = call(http_event("GET", "/v1/personal/profiles", claims=VIEWER, query=demo_q))[1]
    assert len(people["items"]) == 3
    call(
        http_event("PUT", "/v1/personal/profiles", claims=VIEWER, query=demo_q, body={"name": "Yo"})
    )
    own = call(http_event("GET", "/v1/personal/profiles", claims=VIEWER))[1]["items"]
    assert [p["name"] for p in own] == ["Yo"]
    again = call(http_event("GET", "/v1/recurring/personal", claims=VIEWER, query=demo_q))[1]
    assert len(again["profiles"]) == 3


# ---------------------------------------------------------------- dashboard servido por la API
@pytest.fixture
def web_env(memory_env: bootstrap.Container, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pathlib import Path

    from radar_gastos.handlers import web

    monkeypatch.setenv("WEB_ENABLED", "true")
    monkeypatch.setenv("WEB_ROOT", str(Path(__file__).resolve().parents[2] / "frontend"))
    monkeypatch.setenv("COGNITO_DOMAIN_URL", "https://auth.example.com")
    monkeypatch.setenv("SPA_CLIENT_ID", "spa-client")
    web.web_root.cache_clear()
    yield
    web.web_root.cache_clear()


def raw_call(event: dict[str, Any]) -> dict[str, Any]:
    from radar_gastos.handlers import api

    return api.handler(event, Ctx())  # type: ignore[arg-type]


def test_api_serves_dashboard_with_security_headers(web_env: None) -> None:
    result = raw_call(http_event("GET", "/"))
    assert result["statusCode"] == 200
    assert "<title>" in result["body"]
    headers = result["headers"]
    assert headers["content-type"].startswith("text/html")
    assert "script-src 'self';" in headers["content-security-policy"]
    assert "https://auth.example.com" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"

    script = raw_call(http_event("GET", "/vendor/chart.umd.min.js"))
    assert script["headers"]["content-type"].startswith("application/javascript")


def test_api_web_config_points_to_same_origin(web_env: None) -> None:
    result = raw_call(http_event("GET", "/config.json"))
    assert json.loads(result["body"]) == {
        "apiBaseUrl": "",
        "authMode": "cognito",
        "cognitoDomain": "https://auth.example.com",
        "clientId": "spa-client",
    }


def test_api_web_blocks_path_traversal_and_keeps_api_routes(web_env: None) -> None:
    traversal = raw_call(http_event("GET", "/../pyproject.toml"))
    assert "<title>" in traversal["body"]  # devuelve la SPA, nunca archivos de fuera
    status, body = call(http_event("GET", "/v1/health"))
    assert status == 200 and body["status"] == "ok"


def test_api_does_not_serve_dashboard_unless_enabled(memory_env: bootstrap.Container) -> None:
    status, _ = call(http_event("GET", "/"))
    assert status == 404


# ---------------------------------------------------------------- dependencias de AWS caídas
def _client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Query")


def _fail_with(monkeypatch: pytest.MonkeyPatch, c: bootstrap.Container, exc: Exception) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise exc

    monkeypatch.setattr(c.configs, "list_connectors", boom)


def test_ready_checks_the_database(memory_env: bootstrap.Container) -> None:
    status, body = call(http_event("GET", "/v1/ready"))
    assert status == 200
    assert body == {"status": "ok", "checks": {"datos": "ok"}}


def test_ready_reports_503_when_database_denies_access(
    memory_env: bootstrap.Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lo que ocurrió en la prueba de caída: el laboratorio revoca el acceso a DynamoDB.
    _fail_with(monkeypatch, memory_env, _client_error("AccessDeniedException"))
    result = raw_call(http_event("GET", "/v1/ready"))
    assert result["statusCode"] == 503
    assert json.loads(result["body"])["checks"] == {"datos": "no disponible"}
    status, _ = call(http_event("GET", "/v1/health"))
    assert status == 200  # la vivacidad no depende de la base de datos


@pytest.mark.parametrize(
    "code", ["AccessDeniedException", "ThrottlingException", "ServiceUnavailable"]
)
def test_aws_unavailability_returns_clear_503(
    memory_env: bootstrap.Container, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    _fail_with(monkeypatch, memory_env, _client_error(code))
    result = raw_call(http_event("GET", "/v1/connectors", claims=USER))
    assert result["statusCode"] == 503
    assert result["headers"]["Retry-After"] == "30"
    body = json.loads(result["body"])
    assert "no está disponible" in body["message"]
    assert "arn:aws" not in result["body"]  # no filtra detalles internos


def test_connection_error_to_aws_returns_503(
    memory_env: bootstrap.Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import EndpointConnectionError

    _fail_with(monkeypatch, memory_env, EndpointConnectionError(endpoint_url="https://dynamodb"))
    status, body = call(http_event("GET", "/v1/connectors", claims=USER))
    assert status == 503 and "no está disponible" in body["message"]


def test_unexpected_aws_error_is_a_generic_500(
    memory_env: bootstrap.Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_with(monkeypatch, memory_env, _client_error("ValidationException"))
    status, body = call(http_event("GET", "/v1/connectors", claims=USER))
    assert status == 500 and body["message"] == "Error interno"


def test_token_with_invalid_tenant_gets_clear_403(memory_env: bootstrap.Container) -> None:
    # Un usuario creado con 'SaitoCo' (mayúsculas) no puede guardar datos: mejor rechazarlo claro.
    status, body = call(
        http_event("GET", "/v1/me", claims={"sub": "u9", "custom:tenant_id": "SaitoCo"})
    )
    assert status == 403
    assert "minúsculas" in body["message"]
