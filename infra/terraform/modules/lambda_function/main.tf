#
# Módulo reutilizable: función Lambda con rol de mínimo privilegio, logs con
# retención, X-Ray activo y alarma de errores. Graviton (arm64) por costo/rendimiento.
#

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "xray" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "inline" {
  count  = var.policy_json == null ? 0 : 1
  name   = "${var.name}-policy"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "this" {
  function_name                  = var.name
  description                    = var.description
  role                           = aws_iam_role.this.arn
  runtime                        = "python3.13"
  architectures                  = ["arm64"]
  handler                        = var.handler
  filename                       = var.package_path
  source_code_hash               = filebase64sha256(var.package_path)
  layers                         = var.layers
  memory_size                    = var.memory_mb
  timeout                        = var.timeout_seconds
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = merge({
      POWERTOOLS_SERVICE_NAME      = var.name
      POWERTOOLS_METRICS_NAMESPACE = "UsageHub"
      POWERTOOLS_LOG_LEVEL         = var.log_level
    }, var.environment)
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.this.name
  }

  depends_on = [aws_iam_role_policy_attachment.basic, aws_cloudwatch_log_group.this]
  tags       = var.tags
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.name}-errors"
  alarm_description   = "Errores de la función ${var.name}"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.this.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.error_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  tags                = var.tags
}
