"""Integración del ledger contra DynamoDB emulado (moto)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError

from radar_gastos.adapters.dynamodb import (
    ConcurrencyError,
    DynamoConfigRepository,
    DynamoMappingRepository,
    DynamoUsageRepository,
)
from radar_gastos.adapters.keys import fact_key, months_between
from radar_gastos.domain.ledger import Dimension
from radar_gastos.domain.models import (
    Attribution,
    Budget,
    ConnectorConfig,
    PrincipalMapping,
    Provider,
)
from radar_gastos.domain.ports import WriteOutcome
from tests.conftest import RecordFactory

pytestmark = pytest.mark.integration
ALICE = Attribution(member="alice", team="data")
BOB = Attribution(member="bob", team="platform")


def _cost(
    repo: DynamoUsageRepository,
    dimension: Dimension,
    start: date = date(2026, 1, 1),
    end: date = date(2027, 1, 1),
) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for key, m in repo.query_aggregates("acme", start, end, dimension).items():
        out[key.value] = out.get(key.value, Decimal(0)) + m.cost
    return out


def test_upsert_lifecycle(dynamodb_table: tuple[Any, str], make_record: RecordFactory) -> None:
    repo = DynamoUsageRepository(*dynamodb_table)
    assert repo.upsert(make_record(), ALICE) is WriteOutcome.CREATED
    assert repo.upsert(make_record(), ALICE) is WriteOutcome.UNCHANGED
    assert repo.upsert(make_record(cost=Decimal("2.00")), ALICE) is WriteOutcome.REVISED
    assert _cost(repo, Dimension.TEAM) == {"data": Decimal("2.00")}

    assert repo.upsert(make_record(cost=Decimal("2.00")), BOB) is WriteOutcome.REVISED
    assert _cost(repo, Dimension.MEMBER) == {"bob": Decimal("2.00")}  # alice quedó en 0 y se filtra
    assert _cost(repo, Dimension.SERVICE) == {"claude-opus-5-5": Decimal("2.00")}


def test_query_spans_months_and_respects_range(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    repo = DynamoUsageRepository(*dynamodb_table)
    for day in (date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 30), date(2026, 12, 1)):
        repo.upsert(make_record(usage_date=day, cost=Decimal(1)), ALICE)
    assert _cost(repo, Dimension.TEAM, date(2026, 10, 31), date(2026, 12, 1)) == {
        "data": Decimal(3)
    }
    assert _cost(repo, Dimension.TEAM, date(2026, 11, 1), date(2026, 11, 2)) == {"data": Decimal(1)}


def test_months_between() -> None:
    assert list(months_between(date(2026, 11, 20), date(2027, 2, 1))) == [
        "2026-11",
        "2026-12",
        "2027-01",
    ]
    assert list(months_between(date(2026, 11, 1), date(2026, 11, 1))) == []


class _ConflictingClient:
    """Envuelve el cliente real e inyecta N conflictos transaccionales."""

    def __init__(self, inner: Any, conflicts: int, reason: str = "TransactionConflict") -> None:
        self._inner = inner
        self._conflicts = conflicts
        self._reason = reason

    def transact_write_items(self, **kwargs: Any) -> Any:
        if self._conflicts > 0:
            self._conflicts -= 1
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "x"},
                    "CancellationReasons": [{"Code": self._reason}],
                },
                "TransactWriteItems",
            )
        return self._inner.transact_write_items(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_optimistic_retry_recovers_from_conflicts(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    client, table = dynamodb_table
    retries: list[str] = []
    repo = DynamoUsageRepository(
        _ConflictingClient(client, conflicts=3),
        table,
        sleep=lambda _s: None,
        on_retry=retries.append,
    )
    assert repo.upsert(make_record(), ALICE) is WriteOutcome.CREATED
    assert retries == ["TransactionConflict"] * 3
    assert _cost(repo, Dimension.TEAM) == {"data": Decimal("1.25")}


def test_gives_up_after_max_attempts(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    client, table = dynamodb_table
    repo = DynamoUsageRepository(
        _ConflictingClient(client, conflicts=99, reason="ConditionalCheckFailed"),
        table,
        max_attempts=3,
        sleep=lambda _s: None,
    )
    with pytest.raises(ConcurrencyError):
        repo.upsert(make_record(), ALICE)


def test_non_retryable_errors_propagate(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    client, table = dynamodb_table
    repo = DynamoUsageRepository(
        _ConflictingClient(client, conflicts=1, reason="ValidationError"), table
    )
    with pytest.raises(ClientError):
        repo.upsert(make_record(), ALICE)


def test_concurrent_writers_converge(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    """Dos 'workers' leen la misma versión; el segundo pierde la condición y reintenta."""
    client, table = dynamodb_table
    repo = DynamoUsageRepository(client, table, sleep=lambda _s: None)
    repo.upsert(make_record(cost=Decimal(1)), ALICE)

    stale = repo._get_fact
    snapshot = stale(fact_key(make_record()))  # el worker B leyó la versión 1...
    repo.upsert(make_record(cost=Decimal(5)), ALICE)  # versión 2

    calls = {"n": 0}

    def stale_then_fresh(key: dict[str, str]) -> Any:
        calls["n"] += 1
        return snapshot if calls["n"] == 1 else stale(key)

    repo._get_fact = stale_then_fresh  # type: ignore[method-assign]
    assert repo.upsert(make_record(cost=Decimal(7)), ALICE) is WriteOutcome.REVISED
    assert _cost(repo, Dimension.TEAM) == {"data": Decimal(7)}


def test_mapping_and_config_repositories(dynamodb_table: tuple[Any, str]) -> None:
    mappings = DynamoMappingRepository(*dynamodb_table)
    assert mappings.resolve("acme", Provider.OPENAI, "user:x") == Attribution(member="user:x")
    mappings.put_mapping(
        "acme",
        PrincipalMapping(
            provider=Provider.OPENAI, principal="user:x", member="x@acme.com", team="data"
        ),
    )
    assert mappings.resolve("acme", Provider.OPENAI, "user:x") == Attribution(
        member="x@acme.com", team="data"
    )
    assert len(mappings.list_mappings("acme")) == 1

    configs = DynamoConfigRepository(*dynamodb_table)
    configs.put_connector(
        ConnectorConfig(
            tenant_id="acme",
            connector_id="c1",
            provider=Provider.AWS,
            options={"owner_tag": "team"},
        )
    )
    configs.put_connector(
        ConnectorConfig(tenant_id="other", connector_id="c2", provider=Provider.OPENAI)
    )
    assert {c.connector_id for c in configs.list_connectors()} == {"c1", "c2"}
    assert [c.options for c in configs.list_connectors("acme")] == [{"owner_tag": "team"}]

    configs.put_budget(Budget(tenant_id="acme", team="data", monthly_limit=Decimal("100.50")))
    assert configs.list_budgets("acme")[0].monthly_limit == Decimal("100.50")
    assert configs.mark_alert_sent("acme|2026-11#data#*#0.5") is True
    assert configs.mark_alert_sent("acme|2026-11#data#*#0.5") is False


def test_settings_operations_on_dynamodb(dynamodb_table: tuple[Any, str]) -> None:
    from radar_gastos.domain.models import ConnectorStatus

    configs = DynamoConfigRepository(*dynamodb_table)
    mappings = DynamoMappingRepository(*dynamodb_table)
    status = ConnectorStatus(last_sync_at="2026-11-16T10:00:00Z", ok=False, error="401")  # type: ignore[arg-type]

    configs.record_sync("acme", "ghost", status)  # no crea conectores fantasma
    assert configs.list_connectors("acme") == []

    configs.put_connector(
        ConnectorConfig(tenant_id="acme", connector_id="c1", provider=Provider.OPENAI)
    )
    configs.record_sync("acme", "c1", status)
    assert configs.list_statuses("acme")["c1"].error == "401"
    configs.delete_connector("acme", "c1")
    assert configs.list_connectors("acme") == []

    configs.put_budget(
        Budget(tenant_id="acme", team="t", provider=Provider.AWS, monthly_limit=Decimal(5))
    )
    configs.delete_budget("acme", "t", Provider.AWS)
    assert configs.list_budgets("acme") == []

    mappings.put_mapping(
        "acme", PrincipalMapping(provider=Provider.AWS, principal="tag:x", member="x", team="t")
    )
    mappings.delete_mapping("acme", Provider.AWS, "tag:x")
    assert mappings.list_mappings("acme") == []


def test_recurring_repository_on_dynamodb(dynamodb_table: tuple[Any, str]) -> None:
    from radar_gastos.adapters.dynamodb import DynamoRecurringRepository
    from radar_gastos.domain.recurring import RecurringExpense

    repo = DynamoRecurringRepository(*dynamodb_table)
    e = RecurringExpense(
        expense_id="slack-1",
        name="Slack",
        amount=Decimal("8.75"),
        seats=3,
        billing_date=date(2026, 11, 25),
    )
    repo.put_expense("tenant:acme", e)
    assert repo.list_expenses("tenant:acme") == [e]
    assert repo.list_expenses("user:ana") == []
    repo.delete_expense("tenant:acme", "slack-1")
    assert repo.list_expenses("tenant:acme") == []


def test_profiles_on_dynamodb(dynamodb_table: tuple[Any, str]) -> None:
    from radar_gastos.adapters.dynamodb import DynamoRecurringRepository
    from radar_gastos.domain.recurring import Profile

    repo = DynamoRecurringRepository(*dynamodb_table)
    repo.put_profile("user:j", Profile(profile_id="maria-1", name="María"))
    repo.put_profile("user:j", Profile(profile_id="ana-1", name="Ana"))
    assert [p.name for p in repo.list_profiles("user:j")] == ["Ana", "María"]
    repo.delete_profile("user:j", "ana-1")
    assert [p.profile_id for p in repo.list_profiles("user:j")] == ["maria-1"]


# ---------------------------------------------------------------- celdas repartidas en shards
def test_sharded_cells_sum_to_the_same_totals(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    """Con shards, muchos hechos de la misma celda se reparten y la lectura los vuelve a sumar."""
    client, table = dynamodb_table
    sharded = DynamoUsageRepository(client, table, aggregate_shards=8)
    for i in range(40):
        sharded.upsert(make_record(principal=f"user:{i}", cost=Decimal("0.25")), ALICE)

    team_items = client.query(
        TableName=table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": "T#acme#A#team#2026-11"}},
    )["Items"]
    assert len(team_items) > 1  # la celda caliente quedó repartida
    assert _cost(sharded, Dimension.TEAM) == {"data": Decimal("10.00")}
    assert _cost(sharded, Dimension.SERVICE) == {"claude-opus-5-5": Decimal("10.00")}


def test_sharded_revisions_and_reattribution_stay_exact(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    """Un hecho siempre usa el mismo shard: revisarlo o reatribuirlo no deja restos."""
    repo = DynamoUsageRepository(*dynamodb_table, aggregate_shards=8)
    for i in range(10):
        repo.upsert(make_record(principal=f"user:{i}"), ALICE)
    repo.upsert(make_record(principal="user:3", cost=Decimal("5.00")), ALICE)  # revisión
    repo.upsert(make_record(principal="user:4"), BOB)  # reatribución
    assert _cost(repo, Dimension.TEAM) == {
        "data": Decimal("1.25") * 8 + Decimal("5.00"),
        "platform": Decimal("1.25"),
    }


def test_reads_cells_written_before_sharding(
    dynamodb_table: tuple[Any, str], make_record: RecordFactory
) -> None:
    """Las celdas del formato anterior (sin shard ni agg_value) se siguen leyendo y sumando."""
    client, table = dynamodb_table
    DynamoUsageRepository(client, table).upsert(make_record(principal="user:old"), ALICE)
    client.update_item(  # simula una celda antigua: sin el atributo agg_value
        TableName=table,
        Key={"PK": {"S": "T#acme#A#team#2026-11"}, "SK": {"S": "2026-11-10#anthropic#data"}},
        UpdateExpression="REMOVE agg_value",
    )
    sharded = DynamoUsageRepository(client, table, aggregate_shards=8)
    for i in range(5):
        sharded.upsert(make_record(principal=f"user:new{i}"), ALICE)
    assert _cost(sharded, Dimension.TEAM) == {"data": Decimal("1.25") * 6}
