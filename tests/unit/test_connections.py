"""Casos de uso de configuración de conexiones y catálogo de proveedores."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import RecordFactory
from usage_hub.adapters.localfs import FileConfigRepository, FileMappingRepository, FileSecretStore
from usage_hub.adapters.memory import (
    InMemoryConfigRepository,
    InMemoryMappingRepository,
    InMemoryQueue,
    InMemoryUsageRepository,
    StaticSecrets,
)
from usage_hub.connectors.catalog import CATALOG, InvalidConfigurationError, spec_for, split_values
from usage_hub.domain.models import (
    Budget,
    ConnectorConfig,
    ConnectorStatus,
    PrincipalMapping,
    Provider,
    UsageRecord,
)
from usage_hub.domain.ports import CollectionWindow
from usage_hub.services.connections import ConnectionRequest, ConnectionService
from usage_hub.services.ingestion import (
    CollectTask,
    UsageCollector,
    UsageIngestor,
    parse_task,
    safe_error,
)

TODAY = date(2026, 11, 16)
KEY = "sk-ant-admin-SUPERSECRET"


class FakeConnector:
    provider = Provider.ANTHROPIC

    def __init__(self, records: list[UsageRecord], error: Exception | None = None) -> None:
        self.records = records
        self.error = error

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        if self.error:
            raise self.error
        return self.records


@pytest.fixture
def env(make_record: RecordFactory) -> dict[str, object]:
    built: list[Mapping[str, str]] = []
    connector = FakeConnector([make_record(usage_date=TODAY)])

    def builder(config: ConnectorConfig, secret: Mapping[str, str]) -> FakeConnector:
        built.append(dict(secret))
        return connector

    configs, secrets, queue = InMemoryConfigRepository(), StaticSecrets(), InMemoryQueue()
    service = ConnectionService(configs, secrets, queue, lambda: TODAY, builder)
    return {
        "service": service,
        "configs": configs,
        "secrets": secrets,
        "queue": queue,
        "built": built,
        "connector": connector,
    }


def anthropic(values: dict[str, str] | None = None, **kw: object) -> ConnectionRequest:
    return ConnectionRequest(provider=Provider.ANTHROPIC, values=values or {}, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- catálogo
def test_catalog_covers_configurable_providers() -> None:
    assert set(CATALOG) == {
        Provider.ANTHROPIC,
        Provider.OPENAI,
        Provider.AWS,
        Provider.GOOGLE,
        Provider.CLAUDE_CODE,
        Provider.SUBSCRIPTION,
        Provider.SYNTHETIC,
    }
    assert [f.name for f in spec_for(Provider.GOOGLE).secret_fields] == ["service_account_json"]
    assert spec_for(Provider.AWS).as_dict()["provider"] == "aws"
    with pytest.raises(InvalidConfigurationError):
        spec_for(Provider.CUSTOM)


def test_split_values_separates_secrets_and_applies_defaults() -> None:
    secrets, options = split_values(Provider.AWS, {"role_arn": "arn:x", "external_id": ""})
    assert secrets == {"role_arn": "arn:x"}
    assert options == {"owner_tag": "owner"}


def test_split_values_keeps_previous_secret_when_blank() -> None:
    secrets, _ = split_values(Provider.OPENAI, {"admin_api_key": ""}, {"admin_api_key": "old"})
    assert secrets == {"admin_api_key": "old"}


@pytest.mark.parametrize(
    ("provider", "values", "message"),
    [
        (Provider.ANTHROPIC, {}, "obligatorio"),
        (Provider.OPENAI, {"admin_api_key": "k", "evil": "x"}, "desconocidos"),
        (Provider.GOOGLE, {"service_account_json": "{}"}, "Tabla"),
    ],
)
def test_split_values_validation(provider: Provider, values: dict[str, str], message: str) -> None:
    with pytest.raises(InvalidConfigurationError, match=message):
        split_values(provider, values)


# ---------------------------------------------------------------- servicio
def test_save_stores_secret_and_never_returns_it(env: dict[str, object]) -> None:
    service: ConnectionService = env["service"]  # type: ignore[assignment]
    config = service.save("acme", anthropic({"admin_api_key": KEY}, label="Claude prod"))
    assert config.secret_arn and config.connector_id.startswith("claude-prod-")
    listed = service.list("acme")
    assert listed[0]["secrets_set"] == ["admin_api_key"]
    assert KEY not in repr(listed)
    assert env["secrets"].get_secret(config.secret_arn) == {"admin_api_key": KEY}  # type: ignore[attr-defined]


def test_edit_without_secret_keeps_it_and_rotation_replaces_it(env: dict[str, object]) -> None:
    service: ConnectionService = env["service"]  # type: ignore[assignment]
    secrets: StaticSecrets = env["secrets"]  # type: ignore[assignment]
    first = service.save("acme", anthropic({"admin_api_key": KEY}))
    edited = service.save("acme", anthropic({}, connector_id=first.connector_id, lookback_days=7))
    assert edited.secret_arn == first.secret_arn and edited.lookback_days == 7
    rotated = service.save(
        "acme", anthropic({"admin_api_key": "new"}, connector_id=first.connector_id)
    )
    assert rotated.secret_arn != first.secret_arn
    assert secrets.get_secret(first.secret_arn or "") == {}  # el secreto anterior se borró
    with pytest.raises(InvalidConfigurationError, match="proveedor"):
        service.save(
            "acme",
            ConnectionRequest(
                connector_id=first.connector_id,
                provider=Provider.OPENAI,
                values={"admin_api_key": "x"},
            ),
        )


def test_connector_without_secrets_stores_no_secret(env: dict[str, object]) -> None:
    service: ConnectionService = env["service"]  # type: ignore[assignment]
    aws = service.save(
        "acme", ConnectionRequest(provider=Provider.AWS, values={"owner_tag": "team"})
    )
    assert aws.secret_arn is None and aws.options == {"owner_tag": "team"}
    assert service.list("acme")[0]["secrets_set"] == []


def test_test_reports_success_without_writing(env: dict[str, object]) -> None:
    service: ConnectionService = env["service"]  # type: ignore[assignment]
    result = service.test("acme", anthropic({"admin_api_key": KEY}))
    assert result.ok and result.records == 1 and result.cost == Decimal("1.25")
    assert result.as_dict()["window"] == ["2026-11-14", "2026-11-17"]
    assert env["configs"].list_connectors("acme") == []  # type: ignore[attr-defined]


def test_test_reports_errors_without_leaking_secrets(env: dict[str, object]) -> None:
    service: ConnectionService = env["service"]  # type: ignore[assignment]
    env["connector"].error = PermissionError(f"invalid x-api-key {KEY}")  # type: ignore[attr-defined]
    result = service.test("acme", anthropic({"admin_api_key": KEY}))
    assert not result.ok and KEY not in (result.error or "") and "****" in (result.error or "")
    assert not service.test("acme", anthropic({})).ok  # falta la llave


def test_sync_enqueues_clamped_window_and_delete(env: dict[str, object]) -> None:
    service: ConnectionService = env["service"]  # type: ignore[assignment]
    queue: InMemoryQueue = env["queue"]  # type: ignore[assignment]
    config = service.save("acme", anthropic({"admin_api_key": KEY}))
    task = service.sync("acme", config.connector_id, days=400)
    assert (task.end - task.start).days == 31
    assert isinstance(parse_task(queue.messages[0]), CollectTask)
    assert (
        service.sync("acme", config.connector_id).end
        - service.sync("acme", config.connector_id).start
    ).days == 3
    service.delete("acme", config.connector_id)
    assert service.list("acme") == []
    assert env["secrets"].get_secret(config.secret_arn or "") == {}  # type: ignore[attr-defined]


def test_collector_records_sync_status(env: dict[str, object], make_record: RecordFactory) -> None:
    configs: InMemoryConfigRepository = env["configs"]  # type: ignore[assignment]
    service: ConnectionService = env["service"]  # type: ignore[assignment]
    config = service.save("acme", anthropic({"admin_api_key": KEY}))
    collector = UsageCollector(
        configs,
        env["secrets"],
        UsageIngestor(
            InMemoryUsageRepository(),  # type: ignore[arg-type]
            InMemoryMappingRepository(),
        ),
        lambda c, s: env["connector"],
    )  # type: ignore[arg-type,return-value]
    task = CollectTask(
        tenant_id="acme",
        connector_id=config.connector_id,
        start=TODAY,
        end=TODAY + timedelta(days=1),
    )
    collector.handle(task)
    status = configs.list_statuses("acme")[config.connector_id]
    assert status.ok and status.written == 1

    env["connector"].error = RuntimeError(f"boom {KEY}")  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        collector.handle(task)
    status = configs.list_statuses("acme")[config.connector_id]
    assert not status.ok and KEY not in (status.error or "")


def test_safe_error_truncates() -> None:
    assert len(safe_error(ValueError("x" * 1000))) == 300


# ---------------------------------------------------------------- persistencia local
def test_local_files_survive_restart(tmp_path: Path) -> None:
    configs, mappings, secrets = (
        FileConfigRepository(tmp_path),
        FileMappingRepository(tmp_path),
        FileSecretStore(tmp_path),
    )
    config = ConnectorConfig(tenant_id="acme", connector_id="c1", provider=Provider.OPENAI)
    configs.put_connector(config)
    configs.record_sync("acme", "c1", ConnectorStatus(last_sync_at="2026-11-16T00:00:00Z", ok=True))  # type: ignore[arg-type]
    configs.record_sync(
        "acme", "ghost", ConnectorStatus(last_sync_at="2026-11-16T00:00:00Z", ok=True)
    )  # type: ignore[arg-type]
    configs.put_budget(Budget(tenant_id="acme", team="t", monthly_limit=Decimal(10)))
    assert configs.mark_alert_sent("a") and not configs.mark_alert_sent("a")
    mappings.put_mapping(
        "acme", PrincipalMapping(provider=Provider.OPENAI, principal="p", member="m", team="t")
    )
    ref = secrets.put_secret("acme", "c1", {"admin_api_key": "k"})

    configs2, mappings2, secrets2 = (
        FileConfigRepository(tmp_path),
        FileMappingRepository(tmp_path),
        FileSecretStore(tmp_path),
    )
    assert configs2.list_connectors("acme") == [config]
    assert set(configs2.list_statuses("acme")) == {"c1"}  # no resucita conectores inexistentes
    assert configs2.list_budgets("acme")[0].team == "t"
    assert not configs2.mark_alert_sent("a")
    assert mappings2.resolve("acme", Provider.OPENAI, "p").team == "t"
    assert secrets2.get_secret(ref) == {"admin_api_key": "k"}

    configs2.delete_connector("acme", "c1")
    configs2.delete_budget("acme", "t", None)
    mappings2.delete_mapping("acme", Provider.OPENAI, "p")
    secrets2.delete_secret(ref)
    assert FileConfigRepository(tmp_path).list_connectors() == []
    assert FileMappingRepository(tmp_path).list_mappings("acme") == []
    assert FileSecretStore(tmp_path).get_secret(ref) == {}
