"""Diseño de claves de la tabla única de DynamoDB (single-table design).

| Entidad        | PK                                   | SK                               |
|----------------|--------------------------------------|----------------------------------|
| Hecho          | T#{tenant}#F#{yyyy-mm}               | {yyyy-mm-dd}#{record_id}         |
| Agregado       | T#{tenant}#A#{dimension}#{yyyy-mm}   | {yyyy-mm-dd}#{provider}#{valor}  |
| Mapeo          | T#{tenant}#MAP                       | {provider}#{principal}           |
| Conector       | CFG#CONNECTOR                        | {tenant}#{connector_id}          |
| Presupuesto    | T#{tenant}#BUDGET                    | {team}#{provider|*}              |
| Alerta enviada | T#{tenant}#ALERT                     | {yyyy-mm}#{team}#{provider}#{th} |

Particionar por mes acota el tamaño de cada partición y reparte la escritura de
agregados (mitiga *hot partitions*); una consulta de 90 días toca a lo sumo 4
particiones por dimensión.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

from usage_hub.domain.ledger import AggregateKey, Dimension
from usage_hub.domain.models import UsageRecord


def fact_key(record: UsageRecord) -> dict[str, str]:
    month = record.usage_date.strftime("%Y-%m")
    return {
        "PK": f"T#{record.tenant_id}#F#{month}",
        "SK": f"{record.usage_date.isoformat()}#{record.record_id}",
    }


def aggregate_key(tenant_id: str, key: AggregateKey) -> dict[str, str]:
    month = key.usage_date[:7]
    return {
        "PK": f"T#{tenant_id}#A#{key.dimension.value}#{month}",
        "SK": f"{key.usage_date}#{key.provider}#{key.value}",
    }


def parse_aggregate_sk(dimension: Dimension, sk: str) -> AggregateKey:
    usage_date, provider, value = sk.split("#", 2)
    return AggregateKey(usage_date, provider, dimension, value)


def months_between(start: date, end: date) -> Iterator[str]:
    """Meses (yyyy-mm) que intersectan [start, end)."""
    if end <= start:
        return
    last = end - timedelta(days=1)
    year, month = start.year, start.month
    while (year, month) <= (last.year, last.month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:  # noqa: PLR2004
            year, month = year + 1, 1


def mapping_key(tenant_id: str, provider: str, principal: str) -> dict[str, str]:
    return {"PK": f"T#{tenant_id}#MAP", "SK": f"{provider}#{principal}"}


CONNECTOR_PK = "CFG#CONNECTOR"


def connector_key(tenant_id: str, connector_id: str) -> dict[str, str]:
    return {"PK": CONNECTOR_PK, "SK": f"{tenant_id}#{connector_id}"}


def budget_key(tenant_id: str, team: str, provider: str | None) -> dict[str, str]:
    return {"PK": f"T#{tenant_id}#BUDGET", "SK": f"{team}#{provider or '*'}"}


def alert_key(
    tenant_id: str, month: str, team: str, provider: str | None, threshold: str
) -> dict[str, str]:
    return {"PK": f"T#{tenant_id}#ALERT", "SK": f"{month}#{team}#{provider or '*'}#{threshold}"}
