"""Conectores de Claude Code (registros locales), suscripciones y tabla de precios."""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from usage_hub.connectors import build_connector
from usage_hub.connectors.catalog import InvalidConfigurationError, split_values
from usage_hub.connectors.claude_code_local import ClaudeCodeLogConnector
from usage_hub.connectors.pricing import DEFAULT_PRICE, price_for
from usage_hub.domain.models import ConnectorConfig, Provider
from usage_hub.domain.ports import CollectionWindow

WINDOW = CollectionWindow(date(2026, 11, 1), date(2026, 11, 17))


def _line(msg_id: str, ts: str, model: str = "claude-opus-5-5", **usage: object) -> str:
    base = {
        "input_tokens": 100,
        "output_tokens": 1000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1_000_000,
    }
    base.update(usage)
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "requestId": f"req_{msg_id}",
            "message": {"id": msg_id, "model": model, "usage": base},
        }
    )


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "mi-proyecto"
    project.mkdir(parents=True)
    lines = [
        _line("m1", "2026-11-16T10:00:00Z"),
        _line("m1", "2026-11-16T10:00:01Z"),  # misma respuesta en otra línea (streaming)
        _line("m2", "2026-11-16T11:00:00Z", speed="fast"),
        _line("m3", "2026-11-15T09:00:00Z", model="claude-haiku-5-5"),
        _line("m4", "2026-10-01T09:00:00Z"),  # fuera de la ventana
        _line("m5", "2026-11-16T12:00:00Z", model="<synthetic>"),
        json.dumps({"type": "user", "message": {"role": "user", "content": "hola"}}),
        "no es json",
    ]
    (project / "sesion.jsonl").write_text("\n".join(lines), encoding="utf-8")
    old = project / "vieja.jsonl"
    old.write_text(_line("m9", "2026-11-16T10:00:00Z"), encoding="utf-8")
    os.utime(old, (0, 0))  # sin cambios desde 1970: se omite sin leerlo
    return tmp_path


def test_claude_code_dedupes_and_groups_by_day_and_model(claude_home: Path) -> None:
    records = {
        (r.usage_date, r.service): r
        for r in ClaudeCodeLogConnector(claude_home, "ana").fetch("local", WINDOW)
    }
    assert set(records) == {
        (date(2026, 11, 16), "claude-opus-5-5"),
        (date(2026, 11, 15), "claude-haiku-5-5"),
    }
    opus = records[(date(2026, 11, 16), "claude-opus-5-5")]
    assert opus.provider is Provider.CLAUDE_CODE and opus.principal == "claude_code:ana"
    assert opus.requests == 2 and opus.output_tokens == 2000 and opus.cached_tokens == 2_000_000
    one = price_for("claude-opus-5-5").cost(
        input_tokens=100, output_tokens=1000, cache_read_tokens=1_000_000
    )
    assert opus.cost == (one * 3).quantize(Decimal("0.000001"))  # la respuesta fast cuesta el doble


def test_claude_code_missing_folder_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Claude Code"):
        list(ClaudeCodeLogConnector(tmp_path / "nada", "ana").fetch("local", WINDOW))


def test_claude_code_factory_uses_options(claude_home: Path) -> None:
    config = ConnectorConfig(
        tenant_id="local",
        connector_id="cc",
        provider=Provider.CLAUDE_CODE,
        options={"path": str(claude_home), "user": "jimmy"},
    )
    records = list(build_connector(config, {}).fetch("local", WINDOW))
    assert {r.principal for r in records} == {"claude_code:jimmy"}


def test_pricing_table() -> None:
    assert price_for("claude-sonnet-5-5").input == Decimal(2)
    assert price_for("anthropic.claude-haiku-4-5@2025").output == Decimal(5)
    assert price_for("modelo-desconocido") is DEFAULT_PRICE
    cost = price_for("claude-opus-5-5").cost(
        input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
    )
    assert cost == Decimal(5)  # 1,25 × USD 4


def test_subscription_prorates_monthly_fee() -> None:
    config = ConnectorConfig(
        tenant_id="local",
        connector_id="max",
        provider=Provider.SUBSCRIPTION,
        options={
            "vendor": "anthropic",
            "plan": "Claude Max",
            "monthly_cost": "100",
            "member": "jimmy",
            "start_date": "2026-11-10",
        },
    )
    records = list(
        build_connector(config, {}).fetch(
            "local", CollectionWindow(date(2026, 11, 1), date(2026, 12, 1))
        )
    )
    assert len(records) == 21  # del 10 al 30 de noviembre
    assert all(r.provider is Provider.ANTHROPIC for r in records)
    assert (
        records[0].service == "Claude Max (suscripción)"
        and records[0].principal == "subscription:jimmy"
    )
    assert sum(r.cost for r in records).quantize(Decimal("0.01")) == Decimal("70.00")


def test_subscription_rejects_bad_values() -> None:
    bad = ConnectorConfig(
        tenant_id="local",
        connector_id="x",
        provider=Provider.SUBSCRIPTION,
        options={"monthly_cost": "abc"},
    )
    with pytest.raises(ValueError, match="número"):
        build_connector(bad, {})
    zero = bad.model_copy(update={"options": {"monthly_cost": "0"}})
    with pytest.raises(ValueError, match="mayor que cero"):
        build_connector(zero, {})


def test_catalog_validates_select_and_number_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from usage_hub.connectors import catalog as cat

    spec = cat.ProviderSpec(
        provider=Provider.CUSTOM,
        name="Prueba",
        description="",
        docs_url="",
        fields=(
            cat.FieldSpec("tier", "Nivel", kind="select", choices=(("a", "A"), ("b", "B"))),
            cat.FieldSpec("price", "Precio", kind="number"),
        ),
    )
    monkeypatch.setitem(cat.CATALOG, Provider.CUSTOM, spec)
    assert split_values(Provider.CUSTOM, {"tier": "b", "price": "3,5"})[1] == {
        "tier": "b",
        "price": "3,5",
    }
    with pytest.raises(InvalidConfigurationError, match="no válido"):
        split_values(Provider.CUSTOM, {"tier": "z", "price": "1"})
    with pytest.raises(InvalidConfigurationError, match="número"):
        split_values(Provider.CUSTOM, {"tier": "a", "price": "tres"})
    _, cc = split_values(Provider.CLAUDE_CODE, {})
    assert cc == {"path": "~/.claude"}
    assert Provider.SUBSCRIPTION not in cat.CATALOG  # ahora vive en Licencias y suscripciones
