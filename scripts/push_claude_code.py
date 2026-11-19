"""Envía el consumo local de Claude Code de esta persona a una instancia central de Radar de gastos.

Pensado para equipos: cada persona lo ejecuta en su equipo (manualmente o con una tarea
programada diaria) y el consumo queda centralizado por persona. Reenviar es seguro: la
API de ingesta es idempotente y una relectura del mismo día solo actualiza el registro.

    # contra el despliegue en AWS (token client_credentials con scope radar-gastos/ingest)
    python scripts/push_claude_code.py --api https://xxx.execute-api.us-east-1.amazonaws.com \\
        --token "$TOKEN" --user ana@empresa.com --days 7

    # contra el servidor local
    python scripts/push_claude_code.py --api http://localhost:8080 --tenant local --days 30
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from radar_gastos.connectors.claude_code_local import ClaudeCodeLogConnector, resolve_home
from radar_gastos.domain.ports import CollectionWindow

BATCH = 1000


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api", required=True, help="URL base de la API de Radar de gastos")
    parser.add_argument("--token", default="", help="Bearer token (despliegue en AWS)")
    parser.add_argument("--tenant", default="", help="x-tenant-id (solo servidor local)")
    parser.add_argument("--user", default=getpass.getuser(), help="persona a la que se atribuye")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--path", default=None, help="carpeta de Claude Code (por defecto ~/.claude)"
    )
    parser.add_argument("--dry-run", action="store_true", help="muestra lo que se enviaría")
    args = parser.parse_args()

    end = datetime.now(tz=UTC).date() + timedelta(days=1)
    window = CollectionWindow(end - timedelta(days=args.days), end)
    records = list(
        ClaudeCodeLogConnector(resolve_home(args.path), args.user).fetch("local", window)
    )
    payload = [r.model_dump(mode="json", exclude={"tenant_id"}) for r in records]
    total = sum(r.cost for r in records)
    print(
        f"{len(records)} registros diarios, {sum(r.requests for r in records)} respuestas, "
        f"USD {total:.2f} equivalentes en API ({window.start} → {window.end})"
    )
    if args.dry_run or not payload:
        return

    headers = {"content-type": "application/json"}
    if args.token:
        headers["authorization"] = f"Bearer {args.token}"
    if args.tenant:
        headers["x-tenant-id"] = args.tenant
    with httpx.Client(base_url=args.api.rstrip("/"), headers=headers, timeout=30) as client:
        for i in range(0, len(payload), BATCH):
            response = client.post("/v1/usage/records", json={"records": payload[i : i + BATCH]})
            response.raise_for_status()
            print(f"enviados {response.json()['accepted']} registros")


if __name__ == "__main__":
    main()
