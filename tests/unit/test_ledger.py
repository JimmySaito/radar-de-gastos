"""Propiedades del ledger: idempotencia, revisión y reatribución.

Incluye pruebas basadas en propiedades (Hypothesis): para cualquier secuencia de
revisiones y entregas duplicadas, los agregados deben igualar a la última versión.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from radar_gastos.adapters.memory import InMemoryUsageRepository
from radar_gastos.domain.ledger import Dimension, StoredFact, compute_deltas, is_noop
from radar_gastos.domain.models import Attribution, Metrics, Provider, UsageRecord
from radar_gastos.domain.ports import WriteOutcome
from tests.conftest import RecordFactory

ALICE = Attribution(member="alice", team="data")
BOB = Attribution(member="bob", team="platform")


def _totals(repo: InMemoryUsageRepository, dimension: Dimension) -> Metrics:
    total = Metrics()
    for m in repo.query_aggregates("acme", date(2026, 1, 1), date(2027, 1, 1), dimension).values():
        total += m
    return total


def test_first_write_creates_three_aggregate_cells(make_record: RecordFactory) -> None:
    deltas = compute_deltas(None, make_record(), ALICE)
    assert {k.dimension for k in deltas} == {Dimension.MEMBER, Dimension.TEAM, Dimension.SERVICE}
    assert all(d.cost == Decimal("1.25") for d in deltas.values())


def test_identical_rewrite_is_noop(make_record: RecordFactory) -> None:
    record = make_record()
    previous = StoredFact(record, ALICE, 1)
    assert is_noop(previous, record, ALICE)
    assert compute_deltas(previous, record, ALICE) == {}


def test_revision_applies_only_the_difference(make_record: RecordFactory) -> None:
    previous = StoredFact(make_record(cost=Decimal("1.00")), ALICE, 1)
    deltas = compute_deltas(previous, make_record(cost=Decimal("1.75")), ALICE)
    assert {d.cost for d in deltas.values()} == {Decimal("0.75")}


def test_reattribution_moves_value_between_cells(make_record: RecordFactory) -> None:
    record = make_record()
    deltas = compute_deltas(StoredFact(record, ALICE, 1), record, BOB)
    by_value = {(k.dimension, k.value): d.cost for k, d in deltas.items()}
    assert by_value == {
        (Dimension.MEMBER, "alice"): Decimal("-1.25"),
        (Dimension.TEAM, "data"): Decimal("-1.25"),
        (Dimension.MEMBER, "bob"): Decimal("1.25"),
        (Dimension.TEAM, "platform"): Decimal("1.25"),
    }  # la celda de servicio no cambia y se omite


def test_compute_deltas_rejects_different_facts(make_record: RecordFactory) -> None:
    with pytest.raises(ValueError, match="misma clave natural"):
        compute_deltas(StoredFact(make_record(), ALICE, 1), make_record(principal="user:x"), ALICE)


def test_repository_outcomes(make_record: RecordFactory) -> None:
    repo = InMemoryUsageRepository()
    assert repo.upsert(make_record(), ALICE) is WriteOutcome.CREATED
    assert repo.upsert(make_record(), ALICE) is WriteOutcome.UNCHANGED
    assert repo.upsert(make_record(cost=Decimal(3)), ALICE) is WriteOutcome.REVISED
    assert _totals(repo, Dimension.TEAM).cost == Decimal(3)


costs = st.decimals(min_value=Decimal("0.01"), max_value=Decimal(10_000), places=2)


@settings(max_examples=150, deadline=None)
@given(
    revisions=st.lists(
        st.tuples(costs, st.integers(1, 10**9), st.sampled_from([ALICE, BOB]), st.integers(1, 3)),
        min_size=1,
        max_size=12,
    )
)
def test_aggregates_converge_to_latest_version(
    revisions: list[tuple[Decimal, int, Attribution, int]],
) -> None:
    """Invariante: con duplicados (entrega al-menos-una-vez) y revisiones arbitrarias,
    cada dimensión agrega exactamente la última versión del hecho."""
    repo = InMemoryUsageRepository()
    last: UsageRecord | None = None
    last_attr = ALICE
    for cost, tokens, attribution, deliveries in revisions:
        last = UsageRecord(
            tenant_id="acme",
            provider=Provider.OPENAI,
            service="model",
            principal="user:p",
            usage_date=date(2026, 11, 1),
            input_tokens=tokens,
            cost=cost,
        )
        last_attr = attribution
        for _ in range(deliveries):
            repo.upsert(last, attribution)
    assert last is not None
    for dimension in Dimension:
        total = _totals(repo, dimension)
        assert total.cost == last.cost
        assert total.input_tokens == last.input_tokens
    members = repo.query_aggregates("acme", date(2026, 1, 1), date(2027, 1, 1), Dimension.MEMBER)
    assert {k.value for k in members} == {last_attr.member}
