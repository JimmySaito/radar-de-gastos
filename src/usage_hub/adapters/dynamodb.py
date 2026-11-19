"""Adaptador DynamoDB: ledger idempotente con control de concurrencia optimista.

Algoritmo de ``upsert`` (ver ADR-0004):

1. Lectura consistente del hecho previo (versión ``v``).
2. Si el contenido y la atribución no cambiaron → ``UNCHANGED`` (sin escritura).
3. ``compute_deltas`` calcula el ajuste por celda agregada.
4. ``TransactWriteItems`` atómico: Put del hecho con condición
   ``attribute_not_exists(PK)`` o ``version = v`` + ``ADD`` del delta en cada agregado.
5. Si otra ejecución ganó la carrera (``ConditionalCheckFailed``) o hubo conflicto
   transaccional en un agregado caliente (``TransactionConflict``), se reintenta con
   backoff exponencial y *full jitter*.

Garantía resultante: *efecto exactamente-una-vez* sobre los agregados con entrega
*al-menos-una-vez* de SQS.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from usage_hub.adapters import keys
from usage_hub.domain.ledger import AggregateKey, Dimension, StoredFact, compute_deltas, is_noop
from usage_hub.domain.models import (
    Attribution,
    Budget,
    ConnectorConfig,
    ConnectorStatus,
    Metrics,
    PrincipalMapping,
    Provider,
    UsageRecord,
)
from usage_hub.domain.ports import WriteOutcome
from usage_hub.domain.recurring import RecurringExpense

_ser = TypeSerializer()
_deser = TypeDeserializer()
_RETRYABLE_REASONS = {"ConditionalCheckFailed", "TransactionConflict", "ThrottlingError"}


class ConcurrencyError(RuntimeError):
    """Se agotaron los reintentos optimistas; el mensaje volverá a la cola (SQS)."""


def _serialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _ser.serialize(v) for k, v in item.items() if v is not None}


def _deserialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _deser.deserialize(v) for k, v in item.items()}


class DynamoUsageRepository:
    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        max_attempts: int = 8,
        base_delay: float = 0.02,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[str], None] | None = None,
    ) -> None:
        self._db = client
        self._table = table_name
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep
        self._on_retry = on_retry or (lambda _reason: None)

    # ------------------------------------------------------------------ escritura
    def upsert(self, record: UsageRecord, attribution: Attribution) -> WriteOutcome:
        key = keys.fact_key(record)
        for attempt in range(1, self._max_attempts + 1):
            previous = self._get_fact(key)
            if is_noop(previous, record, attribution):
                return WriteOutcome.UNCHANGED
            try:
                self._db.transact_write_items(
                    TransactItems=self._transaction(record, attribution, previous, key)
                )
            except ClientError as exc:
                reason = _cancellation_reason(exc)
                if reason not in _RETRYABLE_REASONS:
                    raise
                self._on_retry(reason)
                if attempt == self._max_attempts:
                    break
                self._sleep(random.uniform(0, self._base_delay * 2**attempt))  # noqa: S311
                continue
            return WriteOutcome.REVISED if previous else WriteOutcome.CREATED
        raise ConcurrencyError(
            f"no se pudo escribir {record.record_id} tras {self._max_attempts} intentos"
        )

    def _transaction(
        self,
        record: UsageRecord,
        attribution: Attribution,
        previous: StoredFact | None,
        key: dict[str, str],
    ) -> list[dict[str, Any]]:
        version = previous.version + 1 if previous else 1
        fact_item = {
            **key,
            "type": "fact",
            **record.model_dump(mode="python"),
            "provider": record.provider.value,
            "usage_date": record.usage_date.isoformat(),
            "member": attribution.member,
            "team": attribution.team,
            "content_hash": record.content_hash,
            "version": version,
        }
        put: dict[str, Any] = {"TableName": self._table, "Item": _serialize(fact_item)}
        if previous is None:
            put["ConditionExpression"] = "attribute_not_exists(PK)"
        else:
            put["ConditionExpression"] = "version = :v"
            put["ExpressionAttributeValues"] = {":v": _ser.serialize(previous.version)}

        items: list[dict[str, Any]] = [{"Put": put}]
        for agg_key, delta in compute_deltas(previous, record, attribution).items():
            items.append({"Update": self._aggregate_update(record.tenant_id, agg_key, delta)})
        return items

    def _aggregate_update(
        self, tenant_id: str, agg_key: AggregateKey, delta: Metrics
    ) -> dict[str, Any]:
        return {
            "TableName": self._table,
            "Key": _serialize(keys.aggregate_key(tenant_id, agg_key)),
            "UpdateExpression": "ADD input_tokens :i, output_tokens :o, cached_tokens :c, "
            "requests :r, cost :k "
            "SET #t = :type",
            "ExpressionAttributeNames": {"#t": "type"},
            "ExpressionAttributeValues": _serialize(
                {
                    ":i": delta.input_tokens,
                    ":o": delta.output_tokens,
                    ":c": delta.cached_tokens,
                    ":r": delta.requests,
                    ":k": delta.cost,
                    ":type": "aggregate",
                }
            ),
        }

    def _get_fact(self, key: dict[str, str]) -> StoredFact | None:
        response = self._db.get_item(
            TableName=self._table, Key=_serialize(key), ConsistentRead=True
        )
        if "Item" not in response:
            return None
        item = _deserialize(response["Item"])
        record = UsageRecord(
            tenant_id=item["tenant_id"],
            provider=Provider(item["provider"]),
            service=item["service"],
            principal=item["principal"],
            usage_date=date.fromisoformat(item["usage_date"]),
            input_tokens=int(item.get("input_tokens", 0)),
            output_tokens=int(item.get("output_tokens", 0)),
            cached_tokens=int(item.get("cached_tokens", 0)),
            requests=int(item.get("requests", 0)),
            cost=Decimal(item.get("cost", 0)),
            currency=item.get("currency", "USD"),
        )
        return StoredFact(
            record, Attribution(member=item["member"], team=item["team"]), int(item["version"])
        )

    # ------------------------------------------------------------------ lectura
    def query_aggregates(
        self, tenant_id: str, start: date, end: date, dimension: Dimension
    ) -> Mapping[AggregateKey, Metrics]:
        result: dict[AggregateKey, Metrics] = {}
        last_day = (end - timedelta(days=1)).isoformat()
        for month in keys.months_between(start, end):
            params: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "PK = :pk AND SK BETWEEN :lo AND :hi",
                "ExpressionAttributeValues": _serialize(
                    {
                        ":pk": f"T#{tenant_id}#A#{dimension.value}#{month}",
                        ":lo": start.isoformat(),
                        ":hi": f"{last_day}#￿",
                    }
                ),
            }
            while True:
                page = self._db.query(**params)
                for raw in page.get("Items", []):
                    item = _deserialize(raw)
                    metrics = Metrics(
                        input_tokens=int(item.get("input_tokens", 0)),
                        output_tokens=int(item.get("output_tokens", 0)),
                        cached_tokens=int(item.get("cached_tokens", 0)),
                        requests=int(item.get("requests", 0)),
                        cost=Decimal(item.get("cost", 0)),
                    )
                    if not metrics.is_zero:
                        result[keys.parse_aggregate_sk(dimension, item["SK"])] = metrics
                if "LastEvaluatedKey" not in page:
                    break
                params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return result


class DynamoMappingRepository:
    def __init__(self, client: Any, table_name: str) -> None:
        self._db = client
        self._table = table_name

    def resolve(self, tenant_id: str, provider: Provider, principal: str) -> Attribution:
        response = self._db.get_item(
            TableName=self._table,
            Key=_serialize(keys.mapping_key(tenant_id, provider.value, principal)),
        )
        if "Item" not in response:
            return Attribution(member=principal)
        item = _deserialize(response["Item"])
        return Attribution(member=item["member"], team=item["team"])

    def put_mapping(self, tenant_id: str, mapping: PrincipalMapping) -> None:
        item = {
            **keys.mapping_key(tenant_id, mapping.provider.value, mapping.principal),
            "type": "mapping",
            **mapping.model_dump(mode="json"),
        }
        self._db.put_item(TableName=self._table, Item=_serialize(item))

    def delete_mapping(self, tenant_id: str, provider: Provider, principal: str) -> None:
        self._db.delete_item(
            TableName=self._table,
            Key=_serialize(keys.mapping_key(tenant_id, provider.value, principal)),
        )

    def list_mappings(self, tenant_id: str) -> Sequence[PrincipalMapping]:
        items = _query_all(self._db, self._table, f"T#{tenant_id}#MAP")
        return [
            PrincipalMapping(
                provider=Provider(i["provider"]),
                principal=i["principal"],
                member=i["member"],
                team=i["team"],
            )
            for i in items
        ]


class DynamoConfigRepository:
    def __init__(self, client: Any, table_name: str) -> None:
        self._db = client
        self._table = table_name

    def list_connectors(self, tenant_id: str | None = None) -> Sequence[ConnectorConfig]:
        prefix = f"{tenant_id}#" if tenant_id else None
        items = _query_all(self._db, self._table, keys.CONNECTOR_PK, sk_prefix=prefix)
        return [ConnectorConfig.model_validate(i["config"]) for i in items]

    def put_connector(self, config: ConnectorConfig) -> None:
        item = {
            **keys.connector_key(config.tenant_id, config.connector_id),
            "type": "connector",
            "config": config.model_dump(mode="json"),
        }
        self._db.put_item(TableName=self._table, Item=_serialize(item))

    def delete_connector(self, tenant_id: str, connector_id: str) -> None:
        self._db.delete_item(
            TableName=self._table, Key=_serialize(keys.connector_key(tenant_id, connector_id))
        )

    def record_sync(self, tenant_id: str, connector_id: str, status: ConnectorStatus) -> None:
        """Actualiza el estado sólo si el conector aún existe (no resucita borrados)."""
        try:
            self._db.update_item(
                TableName=self._table,
                Key=_serialize(keys.connector_key(tenant_id, connector_id)),
                UpdateExpression="SET last_sync = :s",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues=_serialize({":s": status.model_dump(mode="json")}),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def list_statuses(self, tenant_id: str) -> Mapping[str, ConnectorStatus]:
        items = _query_all(self._db, self._table, keys.CONNECTOR_PK, sk_prefix=f"{tenant_id}#")
        return {
            i["config"]["connector_id"]: ConnectorStatus.model_validate(i["last_sync"])
            for i in items
            if i.get("last_sync")
        }

    def list_budgets(self, tenant_id: str) -> Sequence[Budget]:
        items = _query_all(self._db, self._table, f"T#{tenant_id}#BUDGET")
        return [Budget.model_validate(i["budget"]) for i in items]

    def put_budget(self, budget: Budget) -> None:
        item = {
            **keys.budget_key(budget.tenant_id, budget.team, budget.provider),
            "type": "budget",
            "budget": budget.model_dump(mode="json"),
        }
        self._db.put_item(TableName=self._table, Item=_serialize(item))

    def delete_budget(self, tenant_id: str, team: str, provider: Provider | None) -> None:
        self._db.delete_item(
            TableName=self._table, Key=_serialize(keys.budget_key(tenant_id, team, provider))
        )

    def mark_alert_sent(self, alert_id: str) -> bool:
        """Put condicional: garantiza que cada umbral de presupuesto alerte una sola vez."""
        tenant, rest = alert_id.split("|", 1)
        try:
            self._db.put_item(
                TableName=self._table,
                Item=_serialize({"PK": f"T#{tenant}#ALERT", "SK": rest, "type": "alert"}),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True


class DynamoRecurringRepository:
    """Gastos recurrentes: PK ``O#{owner}#RECUR`` · SK ``{expense_id}``."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._db = client
        self._table = table_name

    def list_expenses(self, owner: str) -> Sequence[RecurringExpense]:
        items = _query_all(self._db, self._table, f"O#{owner}#RECUR")
        expenses = [RecurringExpense.model_validate(i["expense"]) for i in items]
        return sorted(expenses, key=lambda e: e.name.lower())

    def put_expense(self, owner: str, expense: RecurringExpense) -> None:
        item = {
            "PK": f"O#{owner}#RECUR",
            "SK": expense.expense_id,
            "type": "recurring",
            "expense": expense.model_dump(mode="json"),
        }
        self._db.put_item(TableName=self._table, Item=_serialize(item))

    def delete_expense(self, owner: str, expense_id: str) -> None:
        self._db.delete_item(
            TableName=self._table, Key=_serialize({"PK": f"O#{owner}#RECUR", "SK": expense_id})
        )


def _query_all(db: Any, table: str, pk: str, sk_prefix: str | None = None) -> list[dict[str, Any]]:
    expression = "PK = :pk"
    values: dict[str, Any] = {":pk": pk}
    if sk_prefix:
        expression += " AND begins_with(SK, :sk)"
        values[":sk"] = sk_prefix
    params: dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": expression,
        "ExpressionAttributeValues": _serialize(values),
    }
    items: list[dict[str, Any]] = []
    while True:
        page = db.query(**params)
        items.extend(_deserialize(i) for i in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _cancellation_reason(exc: ClientError) -> str:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "TransactionCanceledException":
        reasons = [
            r.get("Code")
            for r in exc.response.get("CancellationReasons", [])
            if r.get("Code") != "None"
        ]
        if reasons:
            return str(reasons[0])
        # Algunos emuladores sólo envían el motivo en el mensaje.
        message = exc.response.get("Error", {}).get("Message", "")
        for candidate in _RETRYABLE_REASONS:
            if candidate in message:
                return candidate
        return "TransactionCanceled"
    if code in {"ProvisionedThroughputExceededException", "ThrottlingException"}:
        return "ThrottlingError"
    return code
