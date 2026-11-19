"""Gastos recurrentes: suscripciones personales (streaming, IA, software...) y licencias
empresariales (SaaS por usuario).

Lógica pura: dado un gasto con su ciclo y una fecha de cobro conocida (*ancla*), calcula
los próximos cobros, el equivalente mensual y los resúmenes del panel. No hay conversión
de moneda: los totales se agrupan por moneda para no inventar tipos de cambio.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from radar_gastos.domain.models import CurrencyCode, Identifier

CENT = Decimal("0.01")
UPCOMING_ALERT_DAYS = 3


class Scope(StrEnum):
    """Espacio al que pertenece el gasto: de una persona o de la empresa (tenant)."""

    PERSONAL = "personal"
    BUSINESS = "business"


class BillingCycle(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


# Meses por ciclo (semanal se trata aparte: 52 semanas / 12 meses).
_MONTHS = {BillingCycle.MONTHLY: 1, BillingCycle.QUARTERLY: 3, BillingCycle.ANNUAL: 12}

ExpenseId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")]


class RecurringExpense(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expense_id: ExpenseId
    name: Identifier
    service: str = Field(default="other", max_length=64, description="Clave del catálogo")
    category: str = Field(default="otros", max_length=40)
    plan: str = Field(default="", max_length=120)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: CurrencyCode = "USD"
    cycle: BillingCycle = BillingCycle.MONTHLY
    billing_date: date = Field(description="Una fecha de cobro conocida (pasada o futura)")
    seats: int = Field(default=1, ge=1, le=100_000)
    owner: str = Field(default="", max_length=120, description="Responsable (empresa)")
    team: str = Field(default="", max_length=80)
    active: bool = True
    start_date: date | None = Field(default=None, description="Suscrita desde: no hay cobros antes")
    end_date: date | None = Field(default=None, description="Cancelada desde esta fecha")
    notes: str = Field(default="", max_length=500)
    member_ids: tuple[str, ...] = Field(
        default=(), max_length=20, description="Perfiles (personal)"
    )
    # Fracción del costo que se analiza: 1 = completo; 1/n = la parte de una persona en un
    # gasto compartido entre n. Es interna (vistas por persona), no viene del formulario.
    share: Decimal = Field(default=Decimal(1), gt=0, le=1)

    @property
    def charge(self) -> Decimal:
        """Monto de cada cobro (precio × usuarios × parte analizada)."""
        return (self.amount * self.seats * self.share).quantize(CENT)

    @property
    def shared(self) -> bool:
        return len(self.member_ids) > 1

    @property
    def monthly_equivalent(self) -> Decimal:
        if self.cycle is BillingCycle.WEEKLY:
            return (self.charge * 52 / 12).quantize(CENT)
        return (self.charge / _MONTHS[self.cycle]).quantize(CENT)

    def charges_between(self, start: date, end: date) -> Iterator[date]:
        """Fechas de cobro en [start, end), proyectadas desde la fecha ancla."""
        if not self.active:
            return
        stop = min(end, self.end_date) if self.end_date else end
        if self.start_date and self.start_date > start:
            start = self.start_date
        for day in _occurrences(self.billing_date, self.cycle, start):
            if day >= stop:
                return
            yield day

    def next_charge(self, today: date) -> date | None:
        return next(self.charges_between(today, today + timedelta(days=400)), None)


def _add_months(anchor: date, months: int) -> date:
    """Suma meses respetando fin de mes: un cobro el día 31 cae el 30 o el 28/29."""
    total = anchor.month - 1 + months
    year, month = anchor.year + total // 12, total % 12 + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def _occurrences(anchor: date, cycle: BillingCycle, start: date) -> Iterator[date]:
    """Cobros ≥ start. Se calcula siempre desde el ancla para no acumular desfases."""
    if cycle is BillingCycle.WEEKLY:
        # Primera fecha anchor ± 7k que no es anterior a start (la división entera hace floor).
        day = anchor - timedelta(weeks=(anchor - start).days // 7)
        while True:
            if day >= start:
                yield day
            day += timedelta(weeks=1)
    step = _MONTHS[cycle]
    months_apart = (start.year - anchor.year) * 12 + (start.month - anchor.month)
    k = (months_apart // step) - 1
    while True:
        day = _add_months(anchor, k * step)
        if day >= start:
            yield day
        k += 1


@dataclass(frozen=True, slots=True)
class Charge:
    day: date
    expense: RecurringExpense

    def as_dict(self, today: date) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "days_left": (self.day - today).days,
            "soon": (self.day - today).days <= UPCOMING_ALERT_DAYS,
            "expense_id": self.expense.expense_id,
            "name": self.expense.name,
            "category": self.expense.category,
            "amount": str(self.expense.charge),
            "currency": self.expense.currency,
        }


def upcoming(expenses: Iterable[RecurringExpense], today: date, days: int) -> list[Charge]:
    end = today + timedelta(days=days)
    charges = [Charge(d, e) for e in expenses for d in e.charges_between(today, end)]
    return sorted(charges, key=lambda c: (c.day, c.expense.name))


def summarize(expenses: Sequence[RecurringExpense], today: date) -> dict[str, Any]:
    """Resumen del panel. Todos los montos agrupados por moneda (sin conversión)."""
    active = [e for e in expenses if e.active and (e.end_date is None or e.end_date > today)]
    month_start = today.replace(day=1)
    month_end = _add_months(month_start, 1)

    monthly: defaultdict[str, Decimal] = defaultdict(Decimal)
    this_month: defaultdict[str, Decimal] = defaultdict(Decimal)
    remaining: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_category: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    charges_this_month = 0
    for e in active:
        monthly[e.currency] += e.monthly_equivalent
        by_category[(e.category, e.currency)] += e.monthly_equivalent
        for day in e.charges_between(month_start, month_end):
            charges_this_month += 1
            this_month[e.currency] += e.charge
            if day >= today:
                remaining[e.currency] += e.charge

    def money(values: dict[str, Decimal]) -> dict[str, str]:
        return {c: str(v.quantize(CENT)) for c, v in sorted(values.items())}

    calendar_month = sorted(
        (Charge(d, e) for e in active for d in e.charges_between(month_start, month_end)),
        key=lambda c: (c.day, c.expense.name),
    )
    nxt = upcoming(active, today, 400)
    return {
        "today": today.isoformat(),
        "active_count": len(active),
        "monthly_equivalent": money(monthly),
        "annual_projection": money({c: v * 12 for c, v in monthly.items()}),
        "this_month": {
            "from": month_start.isoformat(),
            "to": month_end.isoformat(),
            "charges": charges_this_month,
            "total": money(this_month),
            "remaining": money(remaining),
            "calendar": [c.as_dict(today) for c in calendar_month],
        },
        "next_charge": nxt[0].as_dict(today) if nxt else None,
        "upcoming": [c.as_dict(today) for c in upcoming(active, today, 31)],
        "by_category": [
            {"category": cat, "currency": cur, "monthly": str(v.quantize(CENT))}
            for (cat, cur), v in sorted(by_category.items(), key=lambda kv: -kv[1])
        ],
    }


# ---------------------------------------------------------------- personas (espacio personal)
UNASSIGNED = "sin-asignar"


class Profile(BaseModel):
    """Una persona del hogar o del grupo (p. ej. Jimmy, María) a la que se asignan gastos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: ExpenseId
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    relation: str = Field(default="", max_length=40)


def for_member(expenses: Iterable[RecurringExpense], member_id: str) -> list[RecurringExpense]:
    """Vista de lo que le corresponde a una persona. Un cobro compartido entre n se reparte con
    ``split_evenly`` (las partes suman exactamente el cobro) y esa parte es la de la persona."""
    if member_id == UNASSIGNED:
        return [e for e in expenses if not e.member_ids]
    view = []
    for e in expenses:
        if member_id not in e.member_ids:
            continue
        full = (e.amount * e.seats).quantize(CENT)
        part = split_evenly(full, len(e.member_ids))[e.member_ids.index(member_id)]
        view.append(e.model_copy(update={"share": part / full}))
    return view


def split_evenly(total: Decimal, parts: int) -> list[Decimal]:
    """Reparte un monto en partes que suman exactamente el total (los centavos sobrantes
    van a los primeros): 100 entre 3 = 33,34 + 33,33 + 33,33."""
    base = (total / parts).quantize(CENT, rounding=ROUND_DOWN)
    leftover = int((total - base * parts) / CENT)
    return [base + (CENT if i < leftover else 0) for i in range(parts)]


def by_member(expenses: Iterable[RecurringExpense], today: date) -> dict[str, dict[str, Any]]:
    """Equivalente mensual y cantidad de suscripciones activas por persona (y sin asignar).

    Se calcula con la misma vista que ``for_member``, así la tarjeta por persona y el panel
    filtrado por esa persona muestran exactamente las mismas cifras."""
    active = [e for e in expenses if e.active and (e.end_date is None or e.end_date > today)]
    members = list(dict.fromkeys(m for e in active for m in e.member_ids))
    if any(not e.member_ids for e in active):
        members.append(UNASSIGNED)
    result: dict[str, dict[str, Any]] = {}
    for m in members:
        view = for_member(active, m)
        monthly: defaultdict[str, Decimal] = defaultdict(Decimal)
        for e in view:
            monthly[e.currency] += e.monthly_equivalent
        result[m] = {
            "count": len(view),
            "shared": sum(1 for e in view if e.shared),
            "monthly": monthly,
        }
    return result
