"""Lambda diaria: evalúa presupuestos de todos los tenants y publica alertas en SNS."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from usage_hub.handlers.bootstrap import container, today_utc
from usage_hub.services.budgets import BudgetEvaluator

logger = Logger(service="usagehub-budget")
tracer = Tracer(service="usagehub-budget")
metrics = Metrics(namespace="UsageHub", service="budget")


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    c = container()
    evaluator = BudgetEvaluator(c.usage, c.configs, c.alert_ledger, c.alerts)
    tenants = sorted(
        {cfg.tenant_id for cfg in c.configs.list_connectors()} | set(event.get("tenants", []))
    )
    sent = sum(evaluator.evaluate(t, today_utc()) for t in tenants)
    metrics.add_metric(name="BudgetAlertsSent", unit=MetricUnit.Count, value=sent)
    return {"tenants": len(tenants), "alerts": sent}
