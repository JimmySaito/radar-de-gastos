from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from radar_gastos.domain.models import Metrics
from tests.conftest import RecordFactory


def test_record_id_is_deterministic_and_ignores_metrics(make_record: RecordFactory) -> None:
    a = make_record(cost=Decimal(1))
    b = make_record(cost=Decimal(2), input_tokens=99)
    assert a.record_id == b.record_id
    assert a.content_hash != b.content_hash


def test_record_id_changes_with_identity(make_record: RecordFactory) -> None:
    assert make_record().record_id != make_record(principal="user:bob").record_id


def test_content_hash_normalizes_decimal_representation(make_record: RecordFactory) -> None:
    assert (
        make_record(cost=Decimal("1.50")).content_hash
        == make_record(cost=Decimal("1.5")).content_hash
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": "ACME"},  # mayúsculas no permitidas
        {"input_tokens": -1},
        {"cost": Decimal("-0.01")},
        {"currency": "usd"},
        {"service": ""},
        {"unexpected": "field"},
    ],
)
def test_invalid_records_are_rejected(
    make_record: RecordFactory, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        make_record(**overrides)


def test_record_without_any_metric_is_rejected(make_record: RecordFactory) -> None:
    with pytest.raises(ValidationError, match="al menos una métrica"):
        make_record(input_tokens=0, output_tokens=0, requests=0, cost=Decimal(0))


def test_metrics_arithmetic(make_record: RecordFactory) -> None:
    m = Metrics.of(make_record())
    assert (m - m).is_zero
    assert (m + m).cost == Decimal("2.50")
    assert m.total_tokens == 1500
    assert Metrics.of(None).is_zero
