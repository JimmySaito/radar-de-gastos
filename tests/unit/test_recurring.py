"""Gastos recurrentes: calendario de cobros, equivalentes mensuales, resumen y casos de uso."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from usage_hub.adapters.localfs import FileRecurringRepository
from usage_hub.adapters.memory import InMemoryRecurringRepository
from usage_hub.domain.recurring import BillingCycle, RecurringExpense, Scope, summarize, upcoming
from usage_hub.domain.recurring_catalog import catalog
from usage_hub.services.recurring import (
    ExpenseNotFoundError,
    InvalidExpenseError,
    RecurringRequest,
    RecurringService,
    owner_key,
)

TODAY = date(2026, 11, 19)


def expense(**kw: Any) -> RecurringExpense:
    values: dict[str, Any] = {
        "expense_id": "netflix",
        "name": "Netflix",
        "amount": Decimal("44900"),
        "currency": "COP",
        "billing_date": date(2026, 11, 21),
    }
    values.update(kw)
    return RecurringExpense(**values)


def dates(e: RecurringExpense, start: date, end: date) -> list[str]:
    return [d.isoformat() for d in e.charges_between(start, end)]


# ---------------------------------------------------------------- calendario de cobros
def test_monthly_charges_clamp_to_month_end_without_drift() -> None:
    e = expense(billing_date=date(2026, 1, 31))
    assert dates(e, date(2026, 1, 1), date(2026, 5, 1)) == [
        "2026-01-31",
        "2026-02-28",
        "2026-03-31",
        "2026-04-30",
    ]


def test_future_anchor_projects_backwards_unless_start_date() -> None:
    e = expense(billing_date=date(2026, 12, 2))
    assert dates(e, date(2026, 11, 1), date(2026, 12, 31)) == ["2026-11-02", "2026-12-02"]
    new = expense(billing_date=date(2026, 12, 2), start_date=date(2026, 12, 2))
    assert dates(new, date(2026, 11, 1), date(2026, 12, 31)) == ["2026-12-02"]


@pytest.mark.parametrize(
    ("cycle", "anchor", "expected"),
    [
        (
            BillingCycle.ANNUAL,
            date(2024, 2, 29),
            ["2025-02-28", "2026-02-28", "2027-02-28", "2028-02-29"],
        ),
        (
            BillingCycle.QUARTERLY,
            date(2026, 1, 15),
            ["2026-01-15", "2026-04-15", "2026-07-15", "2026-10-15"],
        ),
        (
            BillingCycle.WEEKLY,
            date(2026, 11, 20),
            ["2026-11-06", "2026-11-13", "2026-11-20", "2026-11-27"],
        ),
    ],
)
def test_cycles(cycle: BillingCycle, anchor: date, expected: list[str]) -> None:
    start, end = (
        (date(2025, 1, 1), date(2028, 12, 31))
        if cycle is BillingCycle.ANNUAL
        else (
            (date(2026, 1, 1), date(2026, 12, 31))
            if cycle is BillingCycle.QUARTERLY
            else (date(2026, 11, 1), date(2026, 12, 1))
        )
    )
    assert dates(expense(cycle=cycle, billing_date=anchor), start, end) == expected


@given(
    anchor=st.dates(date(2020, 1, 1), date(2030, 12, 31)),
    offset=st.integers(-800, 800),
    cycle=st.sampled_from(list(BillingCycle)),
)
def test_charges_are_increasing_and_inside_window(
    anchor: date, offset: int, cycle: BillingCycle
) -> None:
    from datetime import timedelta

    start = anchor + timedelta(days=offset)
    end = start + timedelta(days=400)
    days = list(expense(cycle=cycle, billing_date=anchor).charges_between(start, end))
    assert days == sorted(set(days))
    assert all(start <= d < end for d in days)
    assert days  # en 400 días siempre hay al menos un cobro, incluso anual


def test_inactive_and_cancelled() -> None:
    assert dates(expense(active=False), date(2026, 1, 1), date(2027, 1, 1)) == []
    cancelled = expense(billing_date=date(2026, 1, 10), end_date=date(2026, 3, 10))
    assert dates(cancelled, date(2026, 1, 1), date(2027, 1, 1)) == ["2026-01-10", "2026-02-10"]
    assert expense(active=False).next_charge(TODAY) is None


def test_monthly_equivalent_and_seats() -> None:
    assert expense(amount=Decimal("120"), cycle=BillingCycle.ANNUAL).monthly_equivalent == Decimal(
        "10.00"
    )
    assert expense(amount=Decimal("10"), cycle=BillingCycle.WEEKLY).monthly_equivalent == Decimal(
        "43.33"
    )
    assert expense(
        amount=Decimal("30"), cycle=BillingCycle.QUARTERLY
    ).monthly_equivalent == Decimal("10.00")
    licence = expense(amount=Decimal("8.75"), currency="USD", seats=12)
    assert licence.charge == Decimal("105.00") and licence.monthly_equivalent == Decimal("105.00")


# ---------------------------------------------------------------- resumen
def test_summary_groups_by_currency_and_flags_soon_charges() -> None:
    items = [
        expense(),  # 21 nov, 44.900 COP
        expense(
            expense_id="spotify",
            name="Spotify",
            amount=Decimal("23900"),
            billing_date=date(2026, 11, 5),
        ),
        expense(
            expense_id="claude",
            name="Claude",
            amount=Decimal("20"),
            currency="USD",
            category="ia",
            billing_date=date(2026, 12, 2),
            start_date=date(2026, 12, 2),
        ),
        expense(
            expense_id="old",
            name="Old",
            amount=Decimal("5"),
            currency="USD",
            end_date=date(2026, 6, 1),
            billing_date=date(2026, 1, 1),
        ),
    ]
    s = summarize(items, TODAY)
    assert s["active_count"] == 3
    assert s["monthly_equivalent"] == {"COP": "68800.00", "USD": "20.00"}
    assert s["annual_projection"] == {"COP": "825600.00", "USD": "240.00"}
    assert s["this_month"]["total"] == {"COP": "68800.00"}  # Claude empieza en diciembre
    assert s["this_month"]["remaining"] == {"COP": "44900.00"}
    assert [c["name"] for c in s["this_month"]["calendar"]] == ["Spotify", "Netflix"]
    assert s["next_charge"]["name"] == "Netflix" and s["next_charge"]["soon"] is True
    assert [c["name"] for c in s["upcoming"]] == ["Netflix", "Claude", "Spotify"]
    assert {r["category"] for r in s["by_category"]} == {"otros", "ia"}


def test_summary_empty() -> None:
    s = summarize([], TODAY)
    assert (
        s["next_charge"] is None and s["monthly_equivalent"] == {} and upcoming([], TODAY, 31) == []
    )


def test_catalogs() -> None:
    personal, business = catalog(Scope.PERSONAL), catalog(Scope.BUSINESS)
    assert {"netflix", "max", "disney", "spotify", "chatgpt", "claude"} <= {
        s["key"] for s in personal["services"]
    }
    assert {"slack", "github", "claude-team"} <= {s["key"] for s in business["services"]}
    assert all(
        s["category"] in {c["key"] for c in personal["categories"]} for s in personal["services"]
    )
    assert all(
        s["category"] in {c["key"] for c in business["categories"]} for s in business["services"]
    )


# ---------------------------------------------------------------- casos de uso
def request(**kw: Any) -> RecurringRequest:
    values: dict[str, Any] = {
        "name": "Netflix",
        "service": "netflix",
        "category": "streaming",
        "amount": "44900",
        "currency": "COP",
        "billing_date": "2026-11-21",
    }
    values.update(kw)
    return RecurringRequest(**values)


def test_service_lifecycle() -> None:
    repo = InMemoryRecurringRepository()
    service = RecurringService(repo, lambda: TODAY)
    owner = owner_key(Scope.PERSONAL, subject="ana", tenant_id="acme")
    assert (
        owner == "user:ana"
        and owner_key(Scope.BUSINESS, subject="ana", tenant_id="acme") == "tenant:acme"
    )

    created = service.save(owner, Scope.PERSONAL, request(seats=5, owner="x", team="y"))
    assert created.expense_id.startswith("netflix-") and created.seats == 1 and created.owner == ""
    edited = service.save(
        owner, Scope.PERSONAL, request(expense_id=created.expense_id, amount="49900")
    )
    assert edited.expense_id == created.expense_id
    overview = service.overview(owner)
    assert len(overview["items"]) == 1 and overview["items"][0]["charge"] == "49900.00"
    assert overview["items"][0]["days_left"] == 2

    service.delete(owner, created.expense_id)
    assert service.overview(owner)["items"] == []
    with pytest.raises(ExpenseNotFoundError):
        service.delete(owner, "nope")


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"category": "colaboracion"}, "categoría"),
        ({"end_date": "2026-11-01"}, "cancelación"),
        ({"start_date": "2026-12-01"}, "inicio"),
        (
            {"start_date": "2026-11-01", "end_date": "2026-11-01", "billing_date": "2026-11-01"},
            "posterior al inicio",
        ),
    ],
)
def test_service_validation(kw: dict[str, str], message: str) -> None:
    with pytest.raises(InvalidExpenseError, match=message):
        RecurringService(InMemoryRecurringRepository(), lambda: TODAY).save(
            "user:a", Scope.PERSONAL, request(**kw)
        )


def test_business_keeps_seats_and_owner() -> None:
    service = RecurringService(InMemoryRecurringRepository(), lambda: TODAY)
    e = service.save(
        "tenant:acme",
        Scope.BUSINESS,
        request(
            name="Slack",
            service="slack",
            category="colaboracion",
            amount="8.75",
            currency="USD",
            seats=12,
            owner="ti",
        ),
    )
    assert (e.seats, e.owner, e.charge) == (12, "ti", Decimal("105.00"))


def test_file_repository_survives_restart(tmp_path: Path) -> None:
    repo = FileRecurringRepository(tmp_path)
    repo.put_expense("user:ana", expense())
    repo.put_expense("tenant:acme", expense(expense_id="slack", name="Slack"))
    again = FileRecurringRepository(tmp_path)
    assert [e.expense_id for e in again.list_expenses("user:ana")] == ["netflix"]
    again.delete_expense("user:ana", "netflix")
    assert FileRecurringRepository(tmp_path).list_expenses("user:ana") == []
    assert len(FileRecurringRepository(tmp_path).list_expenses("tenant:acme")) == 1
