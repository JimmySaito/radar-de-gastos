"""Lambda consumidora de SQS: ejecuta tareas *collect* y *records*.

Usa *partial batch response* (``ReportBatchItemFailures``): sólo los mensajes que
fallan vuelven a la cola; tras ``maxReceiveCount`` intentos van a la DLQ. Gracias al
ledger idempotente, reprocesar un mensaje nunca duplica costos.
"""

from __future__ import annotations

import time
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

from radar_gastos.domain.ports import WriteOutcome
from radar_gastos.handlers.bootstrap import container, on_ledger_retry
from radar_gastos.services.ingestion import CollectTask, UsageCollector, UsageIngestor, parse_task

logger = Logger(service="radar-gastos-collector")
tracer = Tracer(service="radar-gastos-collector")
metrics = Metrics(namespace="RadarDeGastos", service="collector")
processor = BatchProcessor(event_type=EventType.SQS)
on_ledger_retry(
    lambda reason: metrics.add_metric(name=f"LedgerRetry{reason}", unit=MetricUnit.Count, value=1)
)


@tracer.capture_method
def record_handler(record: SQSRecord) -> dict[str, int]:
    c = container()
    task = parse_task(record.body)
    collector = UsageCollector(c.configs, c.secrets, UsageIngestor(c.usage, c.mappings))
    stats = collector.handle(task)

    lag_ms = int(time.time() * 1000) - int(record.attributes.sent_timestamp)
    metrics.add_metric(name="QueueToLedgerLatency", unit=MetricUnit.Milliseconds, value=lag_ms)
    metrics.add_metric(name="RecordsWritten", unit=MetricUnit.Count, value=stats.written)
    metrics.add_metric(
        name="RecordsUnchanged", unit=MetricUnit.Count, value=stats.outcomes[WriteOutcome.UNCHANGED]
    )
    if isinstance(task, CollectTask):
        # Prefijo "records_": "created" es un atributo reservado de logging.LogRecord.
        logger.info(
            "conector recolectado",
            extra={
                "connector": task.connector_id,
                **{f"records_{k}": v for k, v in stats.as_dict().items()},
            },
        )
    return stats.as_dict()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return dict(
        process_partial_response(
            event=event, record_handler=record_handler, processor=processor, context=context
        )
    )
