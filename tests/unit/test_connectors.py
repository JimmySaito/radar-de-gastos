"""Pruebas de contrato de los conectores: respuesta del proveedor â†’ modelo canÃ³nico."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from usage_hub.connectors import build_connector, registered_providers
from usage_hub.connectors.anthropic_admin import AnthropicAnalyticsConnector, actor_principal
from usage_hub.connectors.aws_cost_explorer import AwsCostExplorerConnector
from usage_hub.connectors.openai_admin import OpenAIUsageConnector
from usage_hub.connectors.registry import UnknownProviderError
from usage_hub.connectors.synthetic import SyntheticConnector
from usage_hub.domain.models import ConnectorConfig, Provider
from usage_hub.domain.ports import CollectionWindow, UsageConnector

WINDOW = CollectionWindow(date(2026, 11, 1), date(2026, 11, 3))


class FakeListing:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return self.rows


# ---------------------------------------------------------------- Anthropic
def _ns(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def test_anthropic_merges_usage_and_cost_by_actor_and_model() -> None:
    user = _ns(type="user_actor", user_id="user_01")
    usage_rows = [
        _ns(
            starting_at="2026-11-01T00:00:00Z",
            group=_ns(model="claude-opus-5-5", actor=user),
            usage=_ns(
                uncached_input_tokens=1000,
                cache_creation=_ns(ephemeral_1h_input_tokens=10, ephemeral_5m_input_tokens=20),
                cache_read_input_tokens=300,
                output_tokens=200,
                requests=5,
            ),
        ),
    ]
    cost_rows = [
        _ns(
            starting_at="2026-11-01T00:00:00Z",
            group=_ns(model="claude-opus-5-5", actor=user),
            cost=_ns(currency="USD", amount="1234.500000"),
        ),
    ]
    usage, cost = FakeListing(usage_rows), FakeListing(cost_rows)
    records = list(AnthropicAnalyticsConnector(usage, cost).fetch("acme", WINDOW))

    assert len(records) == 1
    r = records[0]
    assert (r.principal, r.service, r.usage_date) == (
        "user:user_01",
        "claude-opus-5-5",
        date(2026, 11, 1),
    )
    assert (r.input_tokens, r.cached_tokens, r.output_tokens, r.requests) == (1030, 300, 200, 5)
    assert r.cost == Decimal("12.345")  # centavos â†’ USD
    assert usage.calls[0]["group_by"] == ["actor", "model"]
    assert usage.calls[0]["time_bucket_width"] == "1d"
    assert usage.calls[0]["starting_at"] == "2026-11-01T00:00:00Z"


def test_anthropic_splits_windows_longer_than_31_days() -> None:
    usage, cost = FakeListing([]), FakeListing([])
    AnthropicAnalyticsConnector(usage, cost).fetch(
        "acme", CollectionWindow(date(2026, 1, 1), date(2026, 3, 15))
    )
    assert len(usage.calls) == 3


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        (None, "unattributed"),
        (
            {"type": "service_account_actor", "service_account_id": "svac_1"},
            "service_account:svac_1",
        ),
        ({"type": "api_key_actor", "api_key_id": "apikey_9"}, "api_key:apikey_9"),
        ({"type": "weird_actor"}, "weird"),
    ],
)
def test_actor_principal(actor: Any, expected: str) -> None:
    assert actor_principal(actor) == expected


# ---------------------------------------------------------------- OpenAI
@respx.mock
def test_openai_paginates_and_maps_usage_and_costs() -> None:
    base = "https://api.openai.test/v1"
    usage_route = respx.get(f"{base}/organization/usage/completions")
    usage_route.side_effect = [
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": 1793491200,
                        "results": [
                            {
                                "input_tokens": 900,
                                "input_cached_tokens": 100,
                                "output_tokens": 50,
                                "num_model_requests": 3,
                                "user_id": "user-a",
                                "model": "gpt-x",
                            }
                        ],
                    }
                ],
                "has_more": True,
                "next_page": "p2",
            },
        ),
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": 1793491200,
                        "results": [
                            {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "num_model_requests": 1,
                                "api_key_id": "key_1",
                                "model": "gpt-x",
                            }
                        ],
                    }
                ],
                "has_more": False,
            },
        ),
    ]
    respx.get(f"{base}/organization/costs").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": 1793491200,
                        "results": [
                            {
                                "amount": {"value": 0.42, "currency": "usd"},
                                "line_item": "gpt-x, input",
                                "project_id": "proj_1",
                            }
                        ],
                    }
                ],
                "has_more": False,
            },
        )
    )

    connector = OpenAIUsageConnector(httpx.Client(base_url=base))
    records = {r.principal: r for r in connector.fetch("acme", WINDOW)}

    assert records["user:user-a"].input_tokens == 800
    assert records["user:user-a"].cached_tokens == 100
    assert records["api_key:key_1"].requests == 1
    assert records["project:proj_1"].cost == Decimal("0.42")
    assert usage_route.calls[1].request.url.params["page"] == "p2"
    assert usage_route.calls[0].request.url.params.get_list("group_by") == [
        "user_id",
        "api_key_id",
        "model",
    ]


@respx.mock
def test_openai_retries_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    base = "https://api.openai.test/v1"
    respx.get(f"{base}/organization/usage/completions").side_effect = [
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(200, json={"data": [], "has_more": False}),
    ]
    respx.get(f"{base}/organization/costs").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    assert list(OpenAIUsageConnector(httpx.Client(base_url=base)).fetch("acme", WINDOW)) == []


@respx.mock
def test_openai_raises_on_auth_error() -> None:
    base = "https://api.openai.test/v1"
    respx.get(f"{base}/organization/usage/completions").mock(return_value=httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        list(OpenAIUsageConnector(httpx.Client(base_url=base)).fetch("acme", WINDOW))


# ---------------------------------------------------------------- AWS
class FakeCostExplorer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if "NextPageToken" not in kwargs:
            return {
                "NextPageToken": "t2",
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-11-01"},
                        "Groups": [
                            {
                                "Keys": ["Amazon Bedrock", "owner$alice"],
                                "Metrics": {"UnblendedCost": {"Amount": "12.5"}},
                            },
                            {
                                "Keys": ["Amazon EC2", "owner$"],
                                "Metrics": {"UnblendedCost": {"Amount": "3"}},
                            },
                        ],
                    }
                ],
            }
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-11-02"},
                    "Groups": [
                        {"Keys": ["Tax", "owner$"], "Metrics": {"UnblendedCost": {"Amount": "-1"}}},
                    ],
                }
            ]
        }


def test_aws_cost_explorer_maps_tags_and_paginates() -> None:
    ce = FakeCostExplorer()
    records = {
        (r.service, r.principal): r for r in AwsCostExplorerConnector(ce).fetch("acme", WINDOW)
    }
    assert records[("Amazon Bedrock", "tag:alice")].cost == Decimal("12.5")
    assert ("Amazon EC2", "untagged") in records
    assert len(records) == 2  # el crÃ©dito negativo se ignora
    assert ce.requests[1]["NextPageToken"] == "t2"
    assert ce.requests[0]["GroupBy"][1] == {"Type": "TAG", "Key": "owner"}


# ---------------------------------------------------------------- SintÃ©tico y registro
def test_synthetic_is_deterministic() -> None:
    a = list(SyntheticConnector(members=3, seed=1).fetch("acme", WINDOW))
    b = list(SyntheticConnector(members=3, seed=1).fetch("acme", WINDOW))
    assert a == b and len(a) > 0
    assert len({r.record_id for r in a}) == len(a)


def test_registry_builds_lazily_loaded_connectors() -> None:
    config = ConnectorConfig(
        tenant_id="acme", connector_id="s", provider=Provider.SYNTHETIC, options={"members": "2"}
    )
    connector = build_connector(config, {})
    assert isinstance(connector, UsageConnector)
    assert Provider.ANTHROPIC in registered_providers()


def test_registry_builds_real_connectors_without_network() -> None:
    for provider, secret in (
        (Provider.ANTHROPIC, {"admin_api_key": "sk-ant-admin-x"}),
        (Provider.OPENAI, {"admin_api_key": "sk-admin-x"}),
    ):
        config = ConnectorConfig(tenant_id="acme", connector_id="c", provider=provider)
        assert build_connector(config, secret).provider is provider


def test_registry_rejects_unknown_provider() -> None:
    config = ConnectorConfig(tenant_id="acme", connector_id="c", provider=Provider.CUSTOM)
    with pytest.raises(UnknownProviderError):
        build_connector(config, {})


@respx.mock
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            401,
            {"error": {"code": "expired_secret_key", "message": "Your API key has expired."}},
            "la llave venció",
        ),
        (403, {"error": {"code": None, "message": "Insufficient permissions"}}, "sk-admin-"),
        (400, {"error": {"code": "bad", "message": "nope"}}, "OpenAI 400 bad: nope"),
    ],
)
def test_openai_errors_explain_the_cause(status: int, body: dict[str, Any], expected: str) -> None:
    base = "https://api.openai.test/v1"
    respx.get(f"{base}/organization/usage/completions").mock(
        return_value=httpx.Response(status, json=body)
    )
    with pytest.raises(httpx.HTTPStatusError, match=expected):
        list(OpenAIUsageConnector(httpx.Client(base_url=base)).fetch("acme", WINDOW))


def test_record_builder_skips_rows_that_round_to_zero_and_negative_credits() -> None:
    """Regresión: OpenAI devuelve líneas de costo de 1e-7 USD que se redondean a 0."""
    from usage_hub.connectors._util import RecordBuilder

    builder = RecordBuilder("acme", Provider.OPENAI)
    day = date(2026, 11, 2)
    builder.add(day, "gpt, cache writes - 12hr", "project:p", cost=Decimal("0.0000001"))
    builder.add(day, "credito", "project:p", cost=Decimal("-5"))
    builder.add(day, "gpt, input", "project:p", cost=Decimal("0.5"))
    records = list(builder.build())
    assert [(r.service, r.cost) for r in records] == [("gpt, input", Decimal("0.500000"))]


@respx.mock
def test_openai_sync_survives_tiny_cost_lines() -> None:
    base = "https://api.openai.test/v1"
    respx.get(f"{base}/organization/usage/completions").mock(
        return_value=httpx.Response(200, json={"data": [], "has_more": False})
    )
    respx.get(f"{base}/organization/costs").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": 1793491200,
                        "results": [
                            {
                                "amount": {"value": 0.00000004},
                                "line_item": "x, cache writes - 12hr",
                                "project_id": "p",
                            },
                            {"amount": {"value": 1.25}, "line_item": "x, input", "project_id": "p"},
                        ],
                    }
                ],
                "has_more": False,
            },
        )
    )
    records = list(OpenAIUsageConnector(httpx.Client(base_url=base)).fetch("acme", WINDOW))
    assert [r.cost for r in records] == [Decimal("1.250000")]
