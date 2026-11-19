"""Modelo de lectura del dashboard (lado *Query* de CQRS)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from radar_gastos.domain.ledger import Dimension
from radar_gastos.domain.models import Metrics, PrincipalMapping
from radar_gastos.domain.ports import UsageRepository

MAX_RANGE_DAYS = 366


class InvalidRangeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date  # exclusivo

    @classmethod
    def parse(cls, start: str | None, end: str | None, today: date) -> DateRange:
        try:
            end_d = date.fromisoformat(end) if end else today + timedelta(days=1)
            start_d = date.fromisoformat(start) if start else end_d - timedelta(days=30)
        except ValueError as exc:
            raise InvalidRangeError("las fechas deben tener formato YYYY-MM-DD") from exc
        if end_d <= start_d:
            raise InvalidRangeError("'to' debe ser posterior a 'from'")
        if (end_d - start_d).days > MAX_RANGE_DAYS:
            raise InvalidRangeError(f"el rango máximo es de {MAX_RANGE_DAYS} días")
        return cls(start_d, end_d)


def _metrics_json(m: Metrics) -> dict[str, Any]:
    return {
        "cost": str(m.cost.quantize(Decimal("0.01"))),
        "input_tokens": m.input_tokens,
        "output_tokens": m.output_tokens,
        "cached_tokens": m.cached_tokens,
        "total_tokens": m.total_tokens,
        "requests": m.requests,
    }


class DashboardService:
    def __init__(self, repository: UsageRepository) -> None:
        self._repo = repository

    def summary(self, tenant_id: str, rng: DateRange) -> dict[str, Any]:
        """KPIs: total, por proveedor, top equipos, top miembros y top servicios."""
        by_member = self._repo.query_aggregates(tenant_id, rng.start, rng.end, Dimension.MEMBER)
        by_team = self._repo.query_aggregates(tenant_id, rng.start, rng.end, Dimension.TEAM)
        by_service = self._repo.query_aggregates(tenant_id, rng.start, rng.end, Dimension.SERVICE)

        total, providers = Metrics(), defaultdict[str, Metrics](Metrics)
        for key, m in by_member.items():  # cada hecho suma exactamente una vez por dimensión
            total += m
            providers[key.provider] += m

        def top(source: Any, limit: int = 10) -> list[dict[str, Any]]:
            """Top-N por nombre (sumando proveedores), con el desglose por proveedor."""
            grouped: defaultdict[tuple[str, str], Metrics] = defaultdict(Metrics)
            totals: defaultdict[str, Decimal] = defaultdict(Decimal)
            for key, m in source.items():
                grouped[(key.value, key.provider)] += m
                totals[key.value] += m.cost
            leaders = sorted(totals, key=lambda name: totals[name], reverse=True)[:limit]
            rank = {name: i for i, name in enumerate(leaders)}
            rows = sorted(
                ((k, m) for k, m in grouped.items() if k[0] in rank),
                key=lambda kv: (rank[kv[0][0]], kv[0][1]),
            )
            return [
                {"name": name, "provider": prov, **_metrics_json(m)} for (name, prov), m in rows
            ]

        return {
            "tenant_id": tenant_id,
            "from": rng.start.isoformat(),
            "to": rng.end.isoformat(),
            "currency": "USD",
            "total": _metrics_json(total),
            "by_provider": {p: _metrics_json(m) for p, m in sorted(providers.items())},
            "top_teams": top(by_team),
            "top_members": top(by_member),
            "top_services": top(by_service),
        }

    def timeseries(
        self, tenant_id: str, rng: DateRange, dimension: Dimension | None
    ) -> dict[str, Any]:
        """Serie diaria agrupada por proveedor (por defecto) o por una dimensión."""
        source = self._repo.query_aggregates(
            tenant_id, rng.start, rng.end, dimension or Dimension.MEMBER
        )
        series: defaultdict[str, defaultdict[str, Metrics]] = defaultdict(
            lambda: defaultdict(Metrics)
        )
        for key, m in source.items():
            label = key.provider if dimension is None else f"{key.value} ({key.provider})"
            series[label][key.usage_date] += m
        days = [
            (rng.start + timedelta(days=i)).isoformat() for i in range((rng.end - rng.start).days)
        ]
        return {
            "from": rng.start.isoformat(),
            "to": rng.end.isoformat(),
            "group_by": dimension.value if dimension else "provider",
            "days": days,
            "series": [
                {
                    "name": name,
                    "cost": [str(points[d].cost.quantize(Decimal("0.01"))) for d in days],
                    "tokens": [points[d].total_tokens for d in days],
                }
                for name, points in sorted(series.items())
            ],
        }

    def identities(
        self, tenant_id: str, rng: DateRange, mappings: Sequence[PrincipalMapping]
    ) -> list[dict[str, Any]]:
        """Identidades vistas en el rango con su gasto y si ya tienen persona/equipo.

        Alimenta la pantalla de gobierno: lo no asignado aparece primero para que el
        administrador lo mapee (KPI de cobertura de atribución)."""
        mapped = {(m.provider.value, m.member): m for m in mappings}
        totals: defaultdict[tuple[str, str], Metrics] = defaultdict(Metrics)
        for key, m in self._repo.query_aggregates(
            tenant_id, rng.start, rng.end, Dimension.MEMBER
        ).items():
            totals[(key.provider, key.value)] += m
        rows = []
        for (provider, name), m in totals.items():
            mapping = mapped.get((provider, name))
            rows.append(
                {
                    "provider": provider,
                    "name": name,
                    "mapped": mapping is not None,
                    "team": mapping.team if mapping else None,
                    **_metrics_json(m),
                }
            )
        return sorted(rows, key=lambda r: (r["mapped"], -Decimal(r["cost"])))
