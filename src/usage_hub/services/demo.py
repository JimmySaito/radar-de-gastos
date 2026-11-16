"""Datos de la vista *Ejemplo*: un tenant aislado con una organización ficticia.

Se usa igual en local y en AWS: registra un conector sintético realista (el scheduler lo
mantiene al día cada hora), mapeos persona/equipo y presupuestos, y precarga historia.
Es idempotente: ejecutarlo varias veces no duplica nada.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from usage_hub.connectors.synthetic import SyntheticConnector
from usage_hub.domain.models import Budget, ConnectorConfig, PrincipalMapping, Provider
from usage_hub.domain.ports import (
    CollectionWindow,
    ConfigRepository,
    MappingRepository,
    UsageRepository,
)
from usage_hub.services.ingestion import IngestionStats, UsageIngestor

DEMO_TENANT = "demo"
TEAMS = ("plataforma", "datos", "producto", "seguridad")
BUDGETS = (Decimal(1100), Decimal(3000), Decimal(1500), Decimal(900))
DEMO_PROVIDERS = (Provider.ANTHROPIC, Provider.OPENAI, Provider.GOOGLE, Provider.AWS)


def seed_demo(
    usage: UsageRepository,
    mappings: MappingRepository,
    configs: ConfigRepository,
    today: date,
    *,
    tenant_id: str = DEMO_TENANT,
    days: int = 60,
    members: int = 12,
) -> IngestionStats:
    for i in range(members):
        for provider in DEMO_PROVIDERS:
            mappings.put_mapping(
                tenant_id,
                PrincipalMapping(
                    provider=provider,
                    principal=f"user:dev-{i:03d}",
                    member=f"persona{i:02d}@empresa.com",
                    team=TEAMS[i % len(TEAMS)],
                ),
            )
    for team, limit in zip(TEAMS, BUDGETS, strict=True):
        configs.put_budget(Budget(tenant_id=tenant_id, team=team, monthly_limit=limit))
    options = {"members": str(members), "seed": "7", "realistic": "true"}
    configs.put_connector(
        ConnectorConfig(
            tenant_id=tenant_id,
            connector_id="demo-synthetic",
            provider=Provider.SYNTHETIC,
            label="Organización de ejemplo",
            options=options,
        )
    )
    end = today + timedelta(days=1)
    records = SyntheticConnector(members=members, seed=7, realistic=True).fetch(
        tenant_id, CollectionWindow(end - timedelta(days=days), end)
    )
    return UsageIngestor(usage, mappings).ingest(records)
