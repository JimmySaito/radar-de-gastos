"""Caché corta de lecturas de agregados (adapters.cache)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from radar_gastos.adapters.cache import CachedUsageRepository
from radar_gastos.adapters.keys import aggregate_key, shard_for
from radar_gastos.adapters.memory import InMemoryUsageRepository
from radar_gastos.domain.ledger import AggregateKey, Dimension
from radar_gastos.domain.models import Attribution, Provider, UsageRecord
from radar_gastos.services.dashboard import DashboardService, DateRange

ALICE = Attribution(member="alice", team="data")
START, END = date(2026, 11, 1), date(2026, 12, 1)


def record(principal: str, cost: str) -> UsageRecord:
    return UsageRecord(
        tenant_id="acme",
        provider=Provider.OPENAI,
        service="gpt",
        principal=principal,
        usage_date=date(2026, 11, 10),
        cost=Decimal(cost),
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_reads_are_served_from_cache_until_ttl_expires() -> None:
    inner, clock = InMemoryUsageRepository(), Clock()
    repo = CachedUsageRepository(inner, ttl_seconds=15, clock=clock)
    inner.upsert(record("a", "1.00"), ALICE)
    first = repo.query_aggregates("acme", START, END, Dimension.TEAM)

    inner.upsert(record("b", "2.00"), ALICE)  # cambia el dato de fondo
    clock.now = 10
    assert repo.query_aggregates("acme", START, END, Dimension.TEAM) == first  # caché
    clock.now = 16
    (cell,) = repo.query_aggregates("acme", START, END, Dimension.TEAM).values()
    assert cell.cost == Decimal("3.00")  # venció: lectura nueva


def test_cache_is_keyed_by_tenant_range_and_dimension_and_bounded() -> None:
    inner = InMemoryUsageRepository()
    repo = CachedUsageRepository(inner, ttl_seconds=60, max_entries=2)
    inner.upsert(record("a", "1.00"), ALICE)
    assert repo.query_aggregates("acme", START, END, Dimension.TEAM)
    assert repo.query_aggregates("acme", START, END, Dimension.MEMBER)
    assert repo.query_aggregates("otro", START, END, Dimension.TEAM) == {}
    assert len(repo._entries) == 2


def test_writes_pass_through() -> None:
    inner = InMemoryUsageRepository()
    CachedUsageRepository(inner, ttl_seconds=60).upsert(record("a", "1.00"), ALICE)
    assert inner.query_aggregates("acme", START, END, Dimension.TEAM)


def test_summary_total_comes_from_teams_and_matches_members() -> None:
    repo = InMemoryUsageRepository()
    for i in range(30):
        repo.upsert(record(f"user:{i}", "0.50"), Attribution(member=f"m{i}", team="data"))
    summary = DashboardService(repo).summary("acme", DateRange(START, END))
    assert summary["total"]["cost"] == "15.00"
    assert sum(Decimal(m["cost"]) for m in summary["top_members"]) == Decimal("5.00")  # top 10


def test_shard_is_stable_and_in_range() -> None:
    assert shard_for("abc", 1) == 0
    shards = {shard_for(f"r{i}", 8) for i in range(200)}
    assert shards == set(range(8))
    assert shard_for("abc", 8) == shard_for("abc", 8)
    key = AggregateKey("2026-11-10", "openai", Dimension.TEAM, "data")
    assert aggregate_key("acme", key, 0)["SK"] == "2026-11-10#openai#data"
    assert aggregate_key("acme", key, 3)["SK"] == "2026-11-10#openai#data#~3"
