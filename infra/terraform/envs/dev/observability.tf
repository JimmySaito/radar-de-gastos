# ---------------------------------------------------------------- observabilidad
# Alarmas de SLO de la API + dashboard operativo (los paneles de negocio están en la SPA).

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  alarm_description   = "Tasa de errores 5xx de la API > 1% (SLO de disponibilidad 99.9%)."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  evaluation_periods  = 3
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "rate"
    expression  = "100 * errors / MAX([errors, requests])"
    label       = "5xx %"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "5xx"
      dimensions  = { ApiId = aws_apigatewayv2_api.http.id }
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Count"
      dimensions  = { ApiId = aws_apigatewayv2_api.http.id }
      period      = 60
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "api_latency_p95" {
  alarm_name          = "${local.name}-api-latency-p95"
  alarm_description   = "Latencia p95 de la API > 800 ms (objetivo de desempeño)."
  namespace           = "AWS/ApiGateway"
  metric_name         = "Latency"
  dimensions          = { ApiId = aws_apigatewayv2_api.http.id }
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  threshold           = 800
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_dashboard" "ops" {
  dashboard_name = "${local.name}-ops"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "API: solicitudes, 4xx, 5xx"
          region = data.aws_region.current.name
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiId", aws_apigatewayv2_api.http.id],
            [".", "4xx", ".", "."],
            [".", "5xx", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "API: latencia p50 / p95 / p99 (ms)"
          region = data.aws_region.current.name
          period = 60
          metrics = [
            ["AWS/ApiGateway", "Latency", "ApiId", aws_apigatewayv2_api.http.id, { stat = "p50" }],
            ["...", { stat = "p95" }],
            ["...", { stat = "p99" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Cola de trabajo: backlog y antigüedad"
          region = data.aws_region.current.name
          period = 60
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", module.work_queue.queue_name, { stat = "Maximum" }],
            [".", "ApproximateAgeOfOldestMessage", ".", ".", { stat = "Maximum", yAxis = "right" }],
            [".", "ApproximateNumberOfMessagesVisible", ".", module.work_queue.dlq_name, { stat = "Maximum", label = "DLQ" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Colector: concurrencia, throttles, reintentos del ledger"
          region = data.aws_region.current.name
          period = 60
          metrics = [
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", module.collector_fn.function_name, { stat = "Maximum" }],
            [".", "Throttles", ".", ".", { stat = "Sum" }],
            ["RadarDeGastos", "LedgerRetryTransactionConflict", "service", "collector", { stat = "Sum" }],
            ["RadarDeGastos", "LedgerRetryConditionalCheckFailed", "service", "collector", { stat = "Sum" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title  = "Ingesta: registros aceptados vs escritos"
          region = data.aws_region.current.name
          stat   = "Sum"
          period = 60
          metrics = [
            ["RadarDeGastos", "RecordsAccepted", "service", "api"],
            ["RadarDeGastos", "RecordsWritten", "service", "collector"],
            ["RadarDeGastos", "RecordsUnchanged", "service", "collector"],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title  = "Latencia cola → ledger (ms)"
          region = data.aws_region.current.name
          period = 60
          metrics = [
            ["RadarDeGastos", "QueueToLedgerLatency", "service", "collector", { stat = "p50" }],
            ["...", { stat = "p95" }],
            ["...", { stat = "p99" }],
          ]
        }
      },
    ]
  })
}
