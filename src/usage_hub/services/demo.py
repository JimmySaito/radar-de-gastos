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
from usage_hub.domain.recurring import BillingCycle, Profile, RecurringExpense
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


# ---------------------------------------------------------------- ejemplo personal
# Hogar ficticio. Los precios son ilustrativos (no son tarifas oficiales) y las fechas se
# calculan respecto de hoy para que siempre haya cobros próximos y un calendario con datos.
PERSONAL_PEOPLE = (
    ("jimmy", "Jimmy", "Yo"),
    ("maria", "María", "Pareja"),
    ("sofia", "Sofía", "Hija"),
)
_EVERYONE = ("jimmy", "maria", "sofia")
PERSONAL_SUBSCRIPTIONS: tuple[
    tuple[str, str, str, str, str, str, str, int, tuple[str, ...]], ...
] = (
    # (id, nombre, servicio, categoría, plan, monto, moneda, días al cobro, personas): mensuales
    ("netflix", "Netflix", "netflix", "streaming", "Premium", "59900", "COP", 2, _EVERYONE),
    ("spotify", "Spotify", "spotify", "musica", "Familiar", "26900", "COP", 9, _EVERYONE),
    (
        "disney",
        "Disney+",
        "disney",
        "streaming",
        "Estándar",
        "38900",
        "COP",
        15,
        ("maria", "sofia"),
    ),
    ("max", "HBO Max", "max", "streaming", "Estándar", "32900", "COP", -3, ("jimmy", "maria")),
    (
        "youtube",
        "YouTube Premium",
        "youtube",
        "streaming",
        "Individual",
        "25900",
        "COP",
        20,
        ("jimmy",),
    ),
    ("chatgpt", "ChatGPT", "chatgpt", "ia", "Plus", "20", "USD", 1, ("jimmy",)),
    ("claude", "Claude", "claude", "ia", "Pro", "20", "USD", 12, ("jimmy",)),
    ("icloud", "iCloud+", "icloud", "nube", "200 GB", "3.99", "USD", 6, ("maria",)),
    (
        "gamepass",
        "Xbox Game Pass",
        "gamepass",
        "gaming",
        "Ultimate",
        "51900",
        "COP",
        25,
        ("sofia",),
    ),
    ("gym", "Gimnasio", "gym", "bienestar", "", "120000", "COP", -8, ("maria",)),
)
PERSONAL_ANNUAL = (
    ("m365", "Microsoft 365", "m365", "software", "Familia", "99.99", "USD", 60, _EVERYONE),
    (
        "duolingo",
        "Duolingo Super",
        "duolingo",
        "bienestar",
        "Familiar",
        "119.99",
        "USD",
        150,
        ("sofia",),
    ),
)


def personal_demo(today: date) -> tuple[list[Profile], list[RecurringExpense]]:
    """Perfiles y suscripciones del ejemplo personal (solo lectura, generado al vuelo)."""
    profiles = [
        Profile(profile_id=pid, name=name, relation=rel) for pid, name, rel in PERSONAL_PEOPLE
    ]
    expenses = []
    for rows, cycle in (
        (PERSONAL_SUBSCRIPTIONS, BillingCycle.MONTHLY),
        (PERSONAL_ANNUAL, BillingCycle.ANNUAL),
    ):
        for eid, name, service, category, plan, amount, currency, days, members in rows:
            expenses.append(
                RecurringExpense(
                    expense_id=f"demo-{eid}",
                    name=name,
                    service=service,
                    category=category,
                    plan=plan,
                    amount=Decimal(amount),
                    currency=currency,
                    cycle=cycle,
                    billing_date=today + timedelta(days=days),
                    member_ids=members,
                )
            )
    return profiles, expenses
