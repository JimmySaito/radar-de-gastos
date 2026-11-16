"""Pruebas de los handlers de Lambda con eventos reales de API Gateway v2 y SQS."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from usage_hub.adapters.memory import InMemoryQueue
from usage_hub.domain.models import ConnectorConfig, Provider
from usage_hub.handlers import bootstrap

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
M2M = {"client_id": "m2m-client", "scope": "usagehub/ingest"}


def call(event: dict[str, Any]) -> tuple[int, Any]:
    from usage_hub.handlers import api

    result = api.handler(event, Ctx())  # type: ignore[arg-type]
    return result["statusCode"], json.loads(result["body"]) if result.get("body") else None


def drain(c: bootstrap.Container) -> dict[str, Any]:
    """Entrega los mensajes encolados al handler colector como un evento SQS."""
    from usage_hub.handlers import collector

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


def test_admin_configuration_endpoints(memory_env: bootstrap.Container) -> None:
    connector = {"connector_id": "anthropic-main", "provider": "anthropic", "secret_arn": "arn:x"}
    assert call(http_event("PUT", "/v1/connectors", claims=USER, body=connector))[0] == 200
    assert (
        call(http_event("GET", "/v1/connectors", claims=USER))[1]["items"][0]["tenant_id"] == "acme"
    )
    assert (
        call(
            http_event(
                "PUT", "/v1/budgets", claims=USER, body={"team": "data", "monthly_limit": "100"}
            )
        )[0]
        == 200
    )
    items = call(http_event("GET", "/v1/budgets", claims=VIEWER))[1]["items"]
    assert items[0]["spent"] == "0.00"
    assert (
        call(
            http_event(
                "PUT", "/v1/budgets", claims=USER, body={"team": "data", "monthly_limit": "-1"}
            )
        )[0]
        == 400
    )


@pytest.mark.parametrize("query", [{"from": "2026-13-01"}, {"group_by": "planet"}])
def test_bad_queries(memory_env: bootstrap.Container, query: dict[str, str]) -> None:
    assert call(http_event("GET", "/v1/timeseries", claims=VIEWER, query=query))[0] == 400


def test_scheduler_collector_and_budget_handlers(memory_env: bootstrap.Container) -> None:
    from usage_hub.handlers import budget, scheduler

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
    from usage_hub.domain.ledger import Dimension

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
