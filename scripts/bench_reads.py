"""Mide cuántas celdas agregadas lee el dashboard según la cardinalidad de personas.

En AWS la latencia del resumen creció con el número de personas (sección 8.4): cada consulta
recorría todas las celdas del agregado por persona. Este script reproduce ese caso sin la nube,
con el repositorio en memoria, y compara:

  antes   : el resumen leía personas + equipos + servicios
  después : el resumen lee equipos + servicios; el top de personas va en otra petición

Mide celdas leídas (lo que en DynamoDB determina páginas, unidades de lectura y latencia) y el
tiempo de CPU del servicio, que en local no incluye la red.

    python scripts/bench_reads.py --members 1000 10000 30000 60000
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from radar_gastos.adapters.memory import InMemoryUsageRepository
from radar_gastos.domain.ledger import Dimension
from radar_gastos.domain.models import Attribution, Provider, UsageRecord
from radar_gastos.services.dashboard import DashboardService, DateRange

START = date(2026, 11, 1)
RANGE = DateRange(START, START + timedelta(days=30))


class CountingRepository:
    """Cuenta las celdas que devuelve cada consulta de agregados."""

    def __init__(self, inner: InMemoryUsageRepository) -> None:
        self.inner = inner
        self.cells = 0

    def upsert(self, *args: Any) -> Any:
        return self.inner.upsert(*args)

    def query_aggregates(self, tenant_id: str, start: date, end: date, dimension: Dimension) -> Any:
        result = self.inner.query_aggregates(tenant_id, start, end, dimension)
        self.cells += len(result)
        return result


def build(members: int) -> CountingRepository:
    repo = InMemoryUsageRepository()
    for i in range(members):  # como el generador de carga: un principal distinto por registro
        repo.upsert(
            UsageRecord(
                tenant_id="carga",
                provider=Provider.CUSTOM,
                service="loadtest",
                principal=f"user:{i}",
                usage_date=START + timedelta(days=i % 3),
                cost=Decimal("0.01"),
            ),
            Attribution(member=f"user:{i}", team="pruebas"),
        )
    return CountingRepository(repo)


def measure(repo: CountingRepository, call: Any) -> tuple[int, float]:
    repo.cells = 0
    started = time.perf_counter()
    call()
    return repo.cells, round((time.perf_counter() - started) * 1000, 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--members", nargs="+", type=int, default=[1000, 10000, 30000, 60000])
    parser.add_argument("--out", type=Path, default=Path("docs/results/local-lecturas.json"))
    args = parser.parse_args()

    rows = []
    for n in args.members:
        repo = build(n)
        service = DashboardService(repo)  # type: ignore[arg-type]
        new_cells, new_ms = measure(repo, lambda svc=service: svc.summary("carga", RANGE))
        members_cells, members_ms = measure(
            repo, lambda svc=service: svc.top_members("carga", RANGE)
        )
        # Antes, una sola petición hacía ambas lecturas: el resumen esperaba al top de personas.
        rows.append(
            {
                "personas": n,
                "antes_celdas": new_cells + members_cells,
                "antes_ms": round(new_ms + members_ms, 1),
                "despues_resumen_celdas": new_cells,
                "despues_resumen_ms": new_ms,
                "top_personas_celdas": members_cells,
                "top_personas_ms": members_ms,
            }
        )
        print(
            f"{n:>6} personas | antes: {rows[-1]['antes_celdas']:>6} celdas {rows[-1]['antes_ms']:>8} ms"
            f" | resumen ahora: {new_cells:>3} celdas {new_ms:>6} ms | top aparte: {members_cells:>6} celdas {members_ms:>8} ms"
        )
    args.out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
