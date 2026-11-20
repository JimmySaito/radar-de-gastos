from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

import pytest

from radar_gastos.adapters.memory import (
    InMemoryAlerts,
    InMemoryConfigRepository,
    InMemoryMappingRepository,
    InMemoryQueue,
    InMemoryUsageRepository,
    StaticSecrets,
)
from radar_gastos.domain.ledger import Dimension
from radar_gastos.domain.models import (
    Budget,
    ConnectorConfig,
    PrincipalMapping,
    Provider,
    UsageRecord,
)
from radar_gastos.domain.ports import CollectionWindow
from radar_gastos.services.budgets import BudgetEvaluator
from radar_gastos.services.dashboard import DashboardService, DateRange, InvalidRangeError
from radar_gastos.services.ingestion import (
    CollectTask,
    RecordsTask,
    SchedulePlanner,
    UsageCollector,
    UsageIngestor,
    chunk_records,
    parse_task,
)
from tests.conftest import RecordFactory

TODAY = date(2026, 11, 16)


@pytest.fixture
def repos() -> tuple[InMemoryUsageRepository, InMemoryMappingRepository, InMemoryConfigRepository]:
    mappings = InMemoryMappingRepository()
    mappings.put_mapping(
        "acme",
        PrincipalMapping(
            provider=Provider.ANTHROPIC,
            principal="user:alice",
            member="alice@acme.com",
            team="data",
        ),
    )
    return InMemoryUsageRepository(), mappings, InMemoryConfigRepository()


def test_planner_fans_out_enabled_connectors_with_lookback(repos) -> None:  # type: ignore[no-untyped-def]
    _, _, configs = repos
    configs.put_connector(
        ConnectorConfig(
            tenant_id="acme", connector_id="a", provider=Provider.ANTHROPIC, lookback_days=3
        )
    )
    configs.put_connector(
        ConnectorConfig(
            tenant_id="acme", connector_id="off", provider=Provider.OPENAI, enabled=False
        )
    )
    queue = InMemoryQueue()
    assert SchedulePlanner(configs, queue, lambda: TODAY).plan() == 1
    task = parse_task(queue.messages[0])
    assert isinstance(task, CollectTask)
    assert (task.start, task.end) == (date(2026, 11, 14), date(2026, 11, 17))


def test_planner_with_no_connectors_sends_nothing(repos) -> None:  # type: ignore[no-untyped-def]
    assert SchedulePlanner(repos[2], InMemoryQueue(), lambda: TODAY).plan() == 0


def test_ingestor_resolves_attribution(repos, make_record: RecordFactory) -> None:  # type: ignore[no-untyped-def]
    usage, mappings, _ = repos
    stats = UsageIngestor(usage, mappings).ingest(
        [make_record(), make_record(principal="user:unknown")]
    )
    assert stats.written == 2
    members = {
        k.value
        for k in usage.query_aggregates(
            "acme", date(2026, 11, 1), date(2026, 12, 1), Dimension.MEMBER
        )
    }
    assert members == {"alice@acme.com", "user:unknown"}


class StubConnector:
    provider = Provider.ANTHROPIC

    def __init__(self, records: list[UsageRecord]) -> None:
        self.records = records
        self.windows: list[CollectionWindow] = []

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        self.windows.append(window)
        return self.records


def test_collector_pulls_with_secret_and_filters_foreign_tenants(  # type: ignore[no-untyped-def]
    repos, make_record: RecordFactory
) -> None:
    usage, mappings, configs = repos
    configs.put_connector(
        ConnectorConfig(
            tenant_id="acme", connector_id="a", provider=Provider.ANTHROPIC, secret_arn="arn:secret"
        )
    )
    stub = StubConnector([make_record(), make_record(tenant_id="evil")])
    seen_secrets = []

    def builder(config, secret):  # type: ignore[no-untyped-def]
        seen_secrets.append(secret)
        return stub

    collector = UsageCollector(
        configs,
        StaticSecrets({"arn:secret": {"admin_api_key": "k"}}),
        UsageIngestor(usage, mappings),
        builder,
    )
    stats = collector.handle(
        CollectTask(tenant_id="acme", connector_id="a", start=TODAY, end=TODAY + timedelta(1))
    )
    assert stats.written == 1
    assert seen_secrets == [{"admin_api_key": "k"}]


def test_collector_unknown_connector(repos) -> None:  # type: ignore[no-untyped-def]
    usage, mappings, configs = repos
    collector = UsageCollector(configs, StaticSecrets(), UsageIngestor(usage, mappings))
    with pytest.raises(LookupError):
        collector.handle(
            CollectTask(
                tenant_id="acme", connector_id="nope", start=TODAY, end=TODAY + timedelta(1)
            )
        )


def test_records_task_roundtrip_and_chunking(make_record: RecordFactory) -> None:
    records = [make_record(principal=f"user:{i}") for i in range(450)]
    messages = chunk_records(records)
    assert len(messages) == 3
    task = parse_task(messages[0])
    assert isinstance(task, RecordsTask) and len(task.records) == 200


def test_dashboard_summary_and_timeseries(repos, make_record: RecordFactory) -> None:  # type: ignore[no-untyped-def]
    usage, mappings, _ = repos
    UsageIngestor(usage, mappings).ingest(
        [
            make_record(),
            make_record(
                provider=Provider.OPENAI, service="gpt", principal="user:bob", cost=Decimal("2.00")
            ),
            make_record(usage_date=date(2026, 11, 11), cost=Decimal("0.75")),
        ]
    )
    service = DashboardService(usage)
    rng = DateRange(date(2026, 11, 10), date(2026, 11, 12))
    summary = service.summary("acme", rng)
    assert summary["total"]["cost"] == "4.00"
    assert set(summary["by_provider"]) == {"anthropic", "openai"}
    assert "top_members" not in summary  # va aparte: es la consulta que crece con la cardinalidad
    assert service.top_members("acme", rng)["top_members"][0]["name"] == "alice@acme.com"

    ts = service.timeseries("acme", rng, None)
    assert ts["days"] == ["2026-11-10", "2026-11-11"]
    anthropic = next(s for s in ts["series"] if s["name"] == "anthropic")
    assert anthropic["cost"] == ["1.25", "0.75"]
    by_team = service.timeseries("acme", rng, Dimension.TEAM)
    assert by_team["group_by"] == "team"


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-11-10", "2026-11-01", "posterior"),
        ("bad", None, "formato"),
        ("2020-01-01", "2026-01-01", "máximo"),
    ],
)
def test_date_range_validation(start: str, end: str | None, message: str) -> None:
    with pytest.raises(InvalidRangeError, match=message):
        DateRange.parse(start, end, TODAY)


def test_date_range_defaults_to_last_30_days() -> None:
    rng = DateRange.parse(None, None, TODAY)
    assert (rng.end - rng.start).days == 30 and rng.end == TODAY + timedelta(days=1)


def test_budget_alerts_fire_once_per_threshold(repos, make_record: RecordFactory) -> None:  # type: ignore[no-untyped-def]
    usage, mappings, configs = repos
    configs.put_budget(Budget(tenant_id="acme", team="data", monthly_limit=Decimal(10)))
    configs.put_budget(
        Budget(tenant_id="acme", team="data", provider=Provider.OPENAI, monthly_limit=Decimal(1))
    )
    UsageIngestor(usage, mappings).ingest([make_record(cost=Decimal("8.50"), usage_date=TODAY)])
    alerts = InMemoryAlerts()
    evaluator = BudgetEvaluator(usage, configs, configs, alerts)

    assert evaluator.evaluate("acme", TODAY) == 2  # 50% y 80%; el de OpenAI no tiene gasto
    assert evaluator.evaluate("acme", TODAY) == 0  # idempotente
    statuses = {(s.budget.provider, s.spent) for s in evaluator.status("acme", TODAY)}
    assert statuses == {(None, Decimal("8.50")), (Provider.OPENAI, Decimal(0))}
    assert alerts.published[0][1]["threshold"] == "0.5"


def test_demo_seed_is_idempotent() -> None:
    from radar_gastos.services.demo import seed_demo

    usage, mappings, configs = (
        InMemoryUsageRepository(),
        InMemoryMappingRepository(),
        InMemoryConfigRepository(),
    )
    first = seed_demo(usage, mappings, configs, TODAY, days=3, members=2)
    second = seed_demo(usage, mappings, configs, TODAY, days=3, members=2)
    assert first.written > 0 and second.written == 0
    assert [c.connector_id for c in configs.list_connectors("demo")] == ["demo-synthetic"]
    providers = {
        k.provider
        for k in usage.query_aggregates(
            "demo", TODAY - timedelta(days=5), TODAY + timedelta(days=1), Dimension.TEAM
        )
    }
    assert providers == {"anthropic", "openai", "google", "aws"}


def test_identities_lists_unmapped_first(repos, make_record: RecordFactory) -> None:  # type: ignore[no-untyped-def]
    usage, mappings, _ = repos
    UsageIngestor(usage, mappings).ingest(
        [make_record(), make_record(principal="user:bob", cost=Decimal(9))]
    )
    rows = DashboardService(usage).identities(
        "acme", DateRange(date(2026, 11, 1), date(2026, 12, 1)), mappings.list_mappings("acme")
    )
    assert [(r["name"], r["mapped"]) for r in rows] == [
        ("user:bob", False),
        ("alice@acme.com", True),
    ]
    assert rows[1]["team"] == "data"
