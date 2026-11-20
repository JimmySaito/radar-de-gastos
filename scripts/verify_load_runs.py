"""Verifica la exactitud de las pruebas de estrés después de que la cola se vacía.

loadgen.py compara el costo esperado con el observado durante un tiempo limitado; si el ledger
quedó con trabajo pendiente (backlog), esa comparación puede terminar antes de converger. Este
script vuelve a consultar el agregado de cada corrida (servicio ``loadtest-<run_id>``) y anota en
su archivo de resultados el valor final, sin reemplazar la medición original.

    python scripts/verify_load_runs.py docs/results/aws-load.json docs/results/aws-stress.json
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stress_aws import m2m_token, tf_output


def observed_cost(client: httpx.Client, run_id: str, day: str) -> Decimal:
    end = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    response = client.get("/v1/timeseries", params={"from": day, "to": end, "group_by": "service"})
    response.raise_for_status()
    total = Decimal(0)
    for series in response.json()["series"]:
        if series["name"].startswith(f"loadtest-{run_id}"):
            total += sum((Decimal(c) for c in series["cost"]), Decimal(0))
    return total


def main() -> None:
    files = [Path(p) for p in sys.argv[1:]]
    token = m2m_token(
        tf_output("cognito_domain"),
        tf_output("ingest_client_id"),
        tf_output("ingest_client_secret"),
    )
    with httpx.Client(
        base_url=tf_output("api_url"), headers={"authorization": f"Bearer {token}"}, timeout=30
    ) as client:
        for path in files:
            result = json.loads(path.read_text(encoding="utf-8"))
            day = result["executed_at"][:10]
            expected = Decimal(result["consistency"]["expected_cost"])
            observed = observed_cost(client, result["run_id"], day)
            result["consistency_after_drain"] = {
                "checked_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
                "expected_cost": str(expected),
                "observed_cost": str(observed),
                "converged": observed == expected,
            }
            path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            mark = "EXACTO" if observed == expected else f"diferencia {expected - observed}"
            print(f"{path.name}: esperado {expected}  observado {observed}  -> {mark}")


if __name__ == "__main__":
    main()
