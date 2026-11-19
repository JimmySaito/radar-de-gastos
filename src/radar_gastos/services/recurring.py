"""Casos de uso de gastos recurrentes (suscripciones personales y licencias de empresa).

En el espacio personal, cada gasto se puede asignar a una o varias personas (perfiles). Un
gasto compartido entre n personas cuenta 1/n para cada una en las vistas por persona.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from radar_gastos.domain.models import CurrencyCode, Identifier
from radar_gastos.domain.ports import RecurringRepository
from radar_gastos.domain.recurring import (
    CENT,
    UNASSIGNED,
    BillingCycle,
    Profile,
    RecurringExpense,
    Scope,
    by_member,
    for_member,
    summarize,
)
from radar_gastos.domain.recurring_catalog import valid_categories

_SLUG = re.compile(r"[^a-z0-9]+")
DEMO_PERSONAL_OWNER = "demo:personal"


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
    member_ids: list[str] = Field(default_factory=list, max_length=20)


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=60)
    relation: str = Field(default="", max_length=40)


def owner_key(scope: Scope, *, subject: str, tenant_id: str) -> str:
    """Lo personal es de cada persona (``sub`` del token); lo de empresa, del tenant."""
    return f"user:{subject}" if scope is Scope.PERSONAL else f"tenant:{tenant_id}"


def _slug(text: str, fallback: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    base = _SLUG.sub("-", ascii_text.lower()).strip("-")[:40] or fallback
    return f"{base}-{uuid.uuid4().hex[:6]}"


class RecurringService:
    def __init__(self, repository: RecurringRepository, today: Callable[[], date]) -> None:
        self._repo = repository
        self._today = today

    # ------------------------------------------------------------- consulta
    def overview(self, owner: str, member: str | None = None) -> dict[str, Any]:
        """Gastos, perfiles y resumen. Con ``member`` el resumen es la parte de esa persona."""
        today = self._today()
        expenses = list(self._repo.list_expenses(owner))
        profiles = list(self._repo.list_profiles(owner))
        names = {p.profile_id: p.name for p in profiles}
        if member and member != UNASSIGNED and member not in names:
            raise ExpenseNotFoundError(f"no existe la persona {member}")
        view = for_member(expenses, member) if member else expenses

        items = []
        for e in view:
            nxt = e.next_charge(today)
            items.append(
                {
                    **e.model_dump(mode="json", exclude={"share"}),
                    "charge": str(e.charge),
                    "full_charge": str((e.amount * e.seats).quantize(CENT)),
                    "monthly_equivalent": str(e.monthly_equivalent),
                    "next_charge": nxt.isoformat() if nxt else None,
                    "days_left": (nxt - today).days if nxt else None,
                    "member_names": [names.get(m, m) for m in e.member_ids],
                }
            )
        people = by_member(expenses, today)
        by_person = [
            {
                "member_id": p.profile_id,
                "name": p.name,
                "relation": p.relation,
                "count": people.get(p.profile_id, {}).get("count", 0),
                "shared": people.get(p.profile_id, {}).get("shared", 0),
                "monthly": {
                    c: str(v)
                    for c, v in sorted(people.get(p.profile_id, {}).get("monthly", {}).items())
                },
            }
            for p in profiles
        ]
        if UNASSIGNED in people:
            by_person.append(
                {
                    "member_id": UNASSIGNED,
                    "name": "Sin asignar",
                    "relation": "",
                    "count": people[UNASSIGNED]["count"],
                    "shared": 0,
                    "monthly": {
                        c: str(v) for c, v in sorted(people[UNASSIGNED]["monthly"].items())
                    },
                }
            )
        return {
            "member": member,
            "items": items,
            "profiles": [p.model_dump(mode="json") for p in profiles],
            "by_member": by_person,
            "summary": summarize(view, today),
        }

    # ------------------------------------------------------------- gastos
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
        expense_id = (
            request.expense_id if request.expense_id in existing else _slug(request.name, "gasto")
        )
        data = request.model_dump(exclude={"expense_id"})
        if scope is Scope.PERSONAL:  # usuarios, responsable y equipo solo aplican a empresa
            known = {p.profile_id for p in self._repo.list_profiles(owner)}
            members = tuple(dict.fromkeys(request.member_ids))  # sin duplicados, en orden
            if unknown := [m for m in members if m not in known]:
                raise InvalidExpenseError(f"personas desconocidas: {', '.join(unknown)}")
            data.update(seats=1, owner="", team="", member_ids=members)
        else:
            data.update(member_ids=())
        expense = RecurringExpense(expense_id=expense_id, **data)
        self._repo.put_expense(owner, expense)
        return expense

    def delete(self, owner: str, expense_id: str) -> None:
        if expense_id not in {e.expense_id for e in self._repo.list_expenses(owner)}:
            raise ExpenseNotFoundError(f"no existe el gasto {expense_id}")
        self._repo.delete_expense(owner, expense_id)

    # ------------------------------------------------------------- personas
    def save_profile(self, owner: str, request: ProfileRequest) -> Profile:
        name = request.name.strip()
        if not name:
            raise InvalidExpenseError("el nombre de la persona es obligatorio")
        profiles = {p.profile_id: p for p in self._repo.list_profiles(owner)}
        if any(
            p.name.lower() == name.lower() and p.profile_id != request.profile_id
            for p in profiles.values()
        ):
            raise InvalidExpenseError(f"ya existe una persona llamada {name}")
        profile_id = (
            request.profile_id if request.profile_id in profiles else _slug(name, "persona")
        )
        profile = Profile(profile_id=profile_id, name=name, relation=request.relation.strip())
        self._repo.put_profile(owner, profile)
        return profile

    def delete_profile(self, owner: str, profile_id: str) -> int:
        """Elimina la persona y la quita de sus gastos (que pasan a sin asignar o compartidos
        entre el resto). Devuelve cuántos gastos se actualizaron."""
        if profile_id not in {p.profile_id for p in self._repo.list_profiles(owner)}:
            raise ExpenseNotFoundError(f"no existe la persona {profile_id}")
        updated = 0
        for e in self._repo.list_expenses(owner):
            if profile_id in e.member_ids:
                members = tuple(m for m in e.member_ids if m != profile_id)
                self._repo.put_expense(owner, e.model_copy(update={"member_ids": members}))
                updated += 1
        self._repo.delete_profile(owner, profile_id)
        return updated
