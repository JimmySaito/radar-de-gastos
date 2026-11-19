"""Carga la vista *Ejemplo* en un despliegue de AWS (una sola vez; es idempotente).

Registra el conector sintético del tenant ``demo`` (el scheduler lo mantiene al día cada
hora), los mapeos persona/equipo, los presupuestos y 60 días de historia.

    python scripts/seed_demo.py --table radar-gastos-dev --region us-east-1
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import boto3

from radar_gastos.adapters.dynamodb import (
    DynamoConfigRepository,
    DynamoMappingRepository,
    DynamoUsageRepository,
)
from radar_gastos.services.demo import seed_demo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--members", type=int, default=12)
    args = parser.parse_args()
    ddb = boto3.client("dynamodb", region_name=args.region)
    stats = seed_demo(
        DynamoUsageRepository(ddb, args.table),
        DynamoMappingRepository(ddb, args.table),
        DynamoConfigRepository(ddb, args.table),
        datetime.now(tz=UTC).date(),
        days=args.days,
        members=args.members,
    )
    print(f"vista de ejemplo cargada: {stats.as_dict()}")


if __name__ == "__main__":
    main()
