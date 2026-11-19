"""Casos de uso de gastos recurrentes (suscripciones personales y licencias de empresa)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from usage_hub.domain.models import CurrencyCode, Identifier
from usage_hub.domain.ports import RecurringRepository
from usage_hub.domain.recurring import BillingCycle, RecurringExpense, Scope, summarize
from usage_hub.domain.recurring_catalog import valid_categories

_SLUG = re.compile(r"[^a-z0-9]+")


class InvalidExpenseError(ValueError):
    pass


class ExpenseNotFoundError(LookupError):
    pass


class RecurringRequest(BaseModel):
    """Formulario de la UI."""

    model_config = ConfigDict(extra="forbid")

    expense_id: str | None = Field(default=None, max_length=64)
    name: Identifier
    service: str = Field(default="other", max_length=64)
    category: str = Field(default="otros", max_length=40)
    plan: str = Field(default="", max_length=120)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: CurrencyCode = "USD"
    cycle: BillingCycle = BillingCycle.MONTHLY
    billing_date: date
    seats: int = Field(default=1, ge=1, le=100_000)
    owner: str = Field(default="", max_length=120)
    team: str = Field(default="", max_length=80)
    active: bool = True
    start_date: date | None = None
    end_date: date | None = None
    notes: str = Field(default="", max_length=500)


def owner_key(scope: Scope, *, subject: str, tenant_id: str) -> str:
    """Lo personal es de cada persona (``sub`` del token); lo de empresa, del tenant."""
    return f"user:{subject}" if scope is Scope.PERSONAL else f"tenant:{tenant_id}"


class RecurringService:
    def __init__(self, repository: RecurringRepository, today: Callable[[], date]) -> None:
        self._repo = repository
        self._today = today

    def overview(self, owner: str) -> dict[str, Any]:
        today = self._today()
        expenses = self._repo.list_expenses(owner)
        items = []
        for e in expenses:
            nxt = e.next_charge(today)
            items.append(
                {
                    **e.model_dump(mode="json"),
                    "charge": str(e.charge),
                    "monthly_equivalent": str(e.monthly_equivalent),
                    "next_charge": nxt.isoformat() if nxt else None,
                    "days_left": (nxt - today).days if nxt else None,
                }
            )
        return {"items": items, "summary": summarize(expenses, today)}

    def save(self, owner: str, scope: Scope, request: RecurringRequest) -> RecurringExpense:
        if request.category not in valid_categories(scope):
            raise InvalidExpenseError(f"categoría desconocida: {request.category}")
        if request.end_date and request.end_date < request.billing_date:
            raise InvalidExpenseError("la fecha de cancelación es anterior a la fecha de cobro")
        if request.start_date and request.start_date > request.billing_date:
            raise InvalidExpenseError("la fecha de inicio es posterior a la fecha de cobro")
        if request.start_date and request.end_date and request.end_date <= request.start_date:
            raise InvalidExpenseError("la fecha de cancelación debe ser posterior al inicio")
        existing = {e.expense_id for e in self._repo.list_expenses(owner)}
        expense_id = request.expense_id if request.expense_id in existing else self._new_id(request)
        data = request.model_dump(exclude={"expense_id"})
        if scope is Scope.PERSONAL:  # usuarios, responsable y equipo solo aplican a empresa
            data.update(seats=1, owner="", team="")
        expense = RecurringExpense(expense_id=expense_id, **data)
        self._repo.put_expense(owner, expense)
        return expense

    def delete(self, owner: str, expense_id: str) -> None:
        if expense_id not in {e.expense_id for e in self._repo.list_expenses(owner)}:
            raise ExpenseNotFoundError(f"no existe el gasto {expense_id}")
        self._repo.delete_expense(owner, expense_id)

    @staticmethod
    def _new_id(request: RecurringRequest) -> str:
        base = _SLUG.sub("-", request.name.lower()).strip("-")[:40] or "gasto"
        return f"{base}-{uuid.uuid4().hex[:6]}"
