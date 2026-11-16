"""Lambda disparada por EventBridge Scheduler: fan-out de tareas de recolección a SQS."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from usage_hub.handlers.bootstrap import container, today_utc
from usage_hub.services.ingestion import SchedulePlanner

logger = Logger(service="usagehub-scheduler")
tracer = Tracer(service="usagehub-scheduler")
metrics = Metrics(namespace="UsageHub", service="scheduler")


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    c = container()
    planned = SchedulePlanner(c.configs, c.queue, today_utc).plan()
    metrics.add_metric(name="CollectTasksPlanned", unit=MetricUnit.Count, value=planned)
    logger.info("tareas planificadas", extra={"planned": planned})
    return {"planned": planned}
