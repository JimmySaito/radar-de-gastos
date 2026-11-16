"""Conector de Google (Gemini API / Vertex AI) vía Cloud Billing export a BigQuery.

Google no ofrece una API de uso por API key equivalente a las de Anthropic u OpenAI;
la fuente autorizada es la exportación de facturación detallada a BigQuery. El
principal se toma de una etiqueta (``labels.owner``) o, en su defecto, del proyecto.

Dependencia opcional: ``pip install usage-hub[gcp]``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from usage_hub.connectors._util import RecordBuilder, to_date, to_decimal
from usage_hub.connectors.registry import register
from usage_hub.domain.models import ConnectorConfig, Provider, UsageRecord
from usage_hub.domain.ports import CollectionWindow

_TABLE_RE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
DEFAULT_SERVICES = ("Gemini API", "Vertex AI")

_QUERY = """
SELECT
  DATE(usage_start_time) AS usage_date,
  service.description AS service,
  COALESCE(
    (SELECT value FROM UNNEST(labels) WHERE key = @label),
    CONCAT('project:', project.id)
  ) AS principal,
  SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost,
  currency
FROM `{table}`
WHERE usage_start_time >= TIMESTAMP(@start) AND usage_start_time < TIMESTAMP(@end)
  AND service.description IN UNNEST(@services)
GROUP BY usage_date, service, principal, currency
"""


class GoogleBillingConnector:
    provider = Provider.GOOGLE

    def __init__(
        self,
        bq_client: Any,
        table: str,
        label: str = "owner",
        services: tuple[str, ...] = DEFAULT_SERVICES,
    ) -> None:
        if not _TABLE_RE.match(
            table
        ):  # el nombre de tabla no admite parámetros: validar contra inyección
            raise ValueError(f"nombre de tabla BigQuery inválido: {table!r}")
        self._bq = bq_client
        self._table = table
        self._label = label
        self._services = services

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("label", "STRING", self._label),
                bigquery.ScalarQueryParameter("start", "STRING", window.start.isoformat()),
                bigquery.ScalarQueryParameter("end", "STRING", window.end.isoformat()),
                bigquery.ArrayQueryParameter("services", "STRING", list(self._services)),
            ]
        )
        builders: dict[str, RecordBuilder] = {}
        for row in self._bq.query(_QUERY.format(table=self._table), job_config=job_config).result():
            cost = to_decimal(row["net_cost"])
            if cost <= 0:
                continue
            builder = builders.setdefault(
                row["currency"], RecordBuilder(tenant_id, self.provider, row["currency"])
            )
            builder.add(to_date(row["usage_date"]), row["service"], row["principal"], cost=cost)
        return [record for b in builders.values() for record in b.build()]


@register(Provider.GOOGLE)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> GoogleBillingConnector:
    from google.cloud import bigquery
    from google.oauth2 import service_account

    info = json.loads(secret["service_account_json"])
    credentials = service_account.Credentials.from_service_account_info(info)
    client = bigquery.Client(credentials=credentials, project=info["project_id"])
    return GoogleBillingConnector(
        client,
        table=config.options["billing_table"],
        label=config.options.get("owner_label", "owner"),
    )
