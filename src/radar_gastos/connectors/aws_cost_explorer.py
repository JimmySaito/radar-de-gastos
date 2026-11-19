"""Conector de AWS basado en Cost Explorer (``ce:GetCostAndUsage``).

Agrupa por servicio y por un *cost allocation tag* (por defecto ``owner``) que la
organización usa para identificar al responsable de cada recurso. El consumo de
Claude vía Amazon Bedrock aparece aquí como servicio "Amazon Bedrock".

Patrón empresarial: el Lambda asume un rol de sólo lectura en la cuenta de
administración (payer) con ``ExternalId`` para evitar el problema del *confused deputy*.
Cada llamada a Cost Explorer cuesta USD 0,01: el colector la invoca una vez por hora.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from radar_gastos.connectors._util import RecordBuilder, to_date, to_decimal
from radar_gastos.connectors.registry import register
from radar_gastos.domain.models import ConnectorConfig, Provider, UsageRecord
from radar_gastos.domain.ports import CollectionWindow

DEFAULT_TAG = "owner"


class AwsCostExplorerConnector:
    provider = Provider.AWS

    def __init__(
        self, ce_client: Any, owner_tag: str = DEFAULT_TAG, metric: str = "UnblendedCost"
    ) -> None:
        self._ce = ce_client
        self._tag = owner_tag
        self._metric = metric

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        builder = RecordBuilder(tenant_id, self.provider)
        request: dict[str, Any] = {
            "TimePeriod": {"Start": window.start.isoformat(), "End": window.end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": [self._metric],
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "TAG", "Key": self._tag},
            ],
        }
        while True:
            response = self._ce.get_cost_and_usage(**request)
            for result in response.get("ResultsByTime", []):
                day = to_date(result["TimePeriod"]["Start"])
                for group in result.get("Groups", []):
                    service, tag_value = [*group.get("Keys", []), "", ""][:2]
                    owner = tag_value.split("$", 1)[1] if "$" in tag_value else tag_value
                    amount = to_decimal(group["Metrics"][self._metric]["Amount"])
                    if amount <= 0:  # créditos/reembolsos se modelan aparte (fuera de alcance)
                        continue
                    builder.add(day, service, f"tag:{owner}" if owner else "untagged", cost=amount)
            token = response.get("NextPageToken")
            if not token:
                break
            request["NextPageToken"] = token
        return list(builder.build())


@register(Provider.AWS)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> AwsCostExplorerConnector:
    import boto3

    session = boto3.Session()
    if role_arn := secret.get("role_arn"):
        sts = session.client("sts")
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"radar-gastos-{config.tenant_id}",
            ExternalId=secret.get("external_id", config.tenant_id),
            DurationSeconds=900,
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    # Cost Explorer es un servicio global con endpoint en us-east-1.
    ce = session.client("ce", region_name="us-east-1")
    return AwsCostExplorerConnector(ce, owner_tag=config.options.get("owner_tag", DEFAULT_TAG))
