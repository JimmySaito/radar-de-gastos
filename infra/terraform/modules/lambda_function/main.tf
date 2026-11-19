#
# Módulo reutilizable: función Lambda con rol de mínimo privilegio, logs cifrados
# con retención, X-Ray activo y alarma de errores. Graviton (arm64) por costo/rendimiento.
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

locals {
  # Sin role_arn el módulo crea un rol propio de mínimo privilegio; con role_arn usa uno existente
  # (p. ej. LabRole en AWS Academy, donde no se pueden crear roles).
  create_role = var.role_arn == null
  role_arn    = local.create_role ? aws_iam_role.this[0].arn : var.role_arn
}

resource "aws_iam_role" "this" {
  count              = local.create_role ? 1 : 0
  name               = "${var.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "basic" {
  count      = local.create_role ? 1 : 0
  role       = aws_iam_role.this[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "xray" {
  count      = local.create_role && var.tracing_mode == "Active" ? 1 : 0
  role       = aws_iam_role.this[0].name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "inline" {
  count  = local.create_role && var.policy_json != null ? 1 : 0
  name   = "${var.name}-policy"
  role   = aws_iam_role.this[0].id
  policy = var.policy_json
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_lambda_function" "this" {
  # checkov:skip=CKV_AWS_116: la API es síncrona, el colector usa la DLQ de SQS y los horarios reintentan en EventBridge Scheduler
  # checkov:skip=CKV_AWS_117: solo llama a servicios administrados y APIs públicas; una VPC agregaría NAT (costo fijo) sin reducir riesgo
  # checkov:skip=CKV_AWS_272: el paquete lo construye y despliega el mismo pipeline; firma de código recomendada en producción
  function_name                  = var.name
  kms_key_arn                    = var.kms_key_arn
  description                    = var.description
  role                           = local.role_arn
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
      POWERTOOLS_METRICS_NAMESPACE = "RadarDeGastos"
      POWERTOOLS_LOG_LEVEL         = var.log_level
      POWERTOOLS_TRACE_DISABLED    = var.tracing_mode == "Active" ? "false" : "true"
    }, var.environment)
  }

  tracing_config {
    mode = var.tracing_mode
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.this.name
  }

  depends_on = [aws_iam_role_policy_attachment.basic, aws_iam_role_policy.inline, aws_cloudwatch_log_group.this]
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
