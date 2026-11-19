"""Control de gobierno: evaluación de presupuestos mensuales y alertas idempotentes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from radar_gastos.domain.ledger import Dimension
from radar_gastos.domain.models import Budget
from radar_gastos.domain.ports import AlertPublisher, ConfigRepository, UsageRepository


class AlertLedger(Protocol):
    def mark_alert_sent(self, alert_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    budget: Budget
    spent: Decimal

    @property
    def ratio(self) -> Decimal:
        return self.spent / self.budget.monthly_limit

    @property
    def crossed(self) -> tuple[Decimal, ...]:
        return tuple(t for t in self.budget.alert_thresholds if self.ratio >= t)


class BudgetEvaluator:
    def __init__(
        self,
        repository: UsageRepository,
        configs: ConfigRepository,
        alert_ledger: AlertLedger,
        publisher: AlertPublisher,
    ) -> None:
        self._repo = repository
        self._configs = configs
        self._ledger = alert_ledger
        self._publisher = publisher

    def status(self, tenant_id: str, today: date) -> list[BudgetStatus]:
        month_start = today.replace(day=1)
        spent: defaultdict[tuple[str, str | None], Decimal] = defaultdict(Decimal)
        for key, m in self._repo.query_aggregates(
            tenant_id, month_start, today + timedelta(days=1), Dimension.TEAM
        ).items():
            spent[(key.value, key.provider)] += m.cost
            spent[(key.value, None)] += m.cost
        return [
            BudgetStatus(b, spent[(b.team, b.provider.value if b.provider else None)])
            for b in self._configs.list_budgets(tenant_id)
        ]

    def evaluate(self, tenant_id: str, today: date) -> int:
        """Publica una alerta por cada umbral cruzado *una sola vez* por mes."""
        sent = 0
        month = today.strftime("%Y-%m")
        for status in self.status(tenant_id, today):
            for threshold in status.crossed:
                b = status.budget
                provider = b.provider.value if b.provider else "*"
                alert_id = f"{tenant_id}|{month}#{b.team}#{provider}#{threshold}"
                if not self._ledger.mark_alert_sent(alert_id):
                    continue
                self._publisher.publish(
                    f"[Radar de gastos] {b.team} alcanzó {int(threshold * 100)}% del presupuesto",
                    {
                        "tenant_id": tenant_id,
                        "team": b.team,
                        "provider": provider,
                        "month": month,
                        "spent": str(status.spent.quantize(Decimal("0.01"))),
                        "limit": str(b.monthly_limit),
                        "threshold": str(threshold),
                    },
                )
                sent += 1
        return sent
