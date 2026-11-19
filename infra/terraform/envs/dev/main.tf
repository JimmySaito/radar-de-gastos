locals {
  name = "radar-gastos-${var.environment}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ---------------------------------------------------------------- datos
# Tabla única (single-table design). On-demand: escala con la carga sin planificar
# capacidad; PITR para recuperación ante borrados; cifrado en reposo con KMS de AWS.
resource "aws_dynamodb_table" "main" {
  name                        = local.name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "PK"
  range_key                   = "SK"
  deletion_protection_enabled = var.enable_deletion_protection

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ---------------------------------------------------------------- mensajería
resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

module "work_queue" {
  source                     = "../../modules/sqs_queue"
  name                       = "${local.name}-work"
  visibility_timeout_seconds = 6 * 60 # 6x el timeout del colector (60 s)
  max_receive_count          = 5
  alarm_actions              = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------- cómputo
resource "aws_lambda_layer_version" "deps" {
  layer_name               = "${local.name}-deps"
  filename                 = var.lambda_layer_package
  source_code_hash         = filebase64sha256(var.lambda_layer_package)
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]
}

locals {
  common_env = {
    TABLE_NAME = aws_dynamodb_table.main.name
    QUEUE_URL  = module.work_queue.queue_url
  }
  table_arn = aws_dynamodb_table.main.arn
}

module "api_fn" {
  source          = "../../modules/lambda_function"
  name            = "${local.name}-api"
  description     = "API HTTP: dashboard, ingesta push y gobierno"
  handler         = "radar_gastos.handlers.api.handler"
  package_path    = var.lambda_package
  layers          = [aws_lambda_layer_version.deps.arn]
  memory_mb       = 1024 # más memoria = más CPU = menor latencia p99 en agregaciones
  timeout_seconds = 29   # "Probar conexión" llama al proveedor; límite de integración de HTTP API: 30 s
  alarm_actions   = [aws_sns_topic.alerts.arn]
  environment = merge(local.common_env, {
    AUTH_MODE          = "jwt"
    DEMO_TENANT        = "demo"
    M2M_CLIENT_TENANTS = jsonencode({ (aws_cognito_user_pool_client.ingest.id) = var.m2m_tenant })
  })
  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = local.table_arn
      },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = module.work_queue.queue_arn },
      {
        # Pantalla de Configuración: guarda/rota/borra credenciales de conectores.
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:GetSecretValue",
          "secretsmanager:DeleteSecret", "secretsmanager:TagResource",
        ]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:radar-gastos/*"
      },
      # "Probar conexión" del conector AWS.
      { Effect = "Allow", Action = ["ce:GetCostAndUsage"], Resource = "*" },
      { Effect = "Allow", Action = ["sts:AssumeRole"], Resource = "arn:aws:iam::*:role/radar-gastos-cost-reader" },
    ]
  })
}

module "collector_fn" {
  source          = "../../modules/lambda_function"
  name            = "${local.name}-collector"
  description     = "Consumidor SQS: recolecta proveedores y escribe el ledger idempotente"
  handler         = "radar_gastos.handlers.collector.handler"
  package_path    = var.lambda_package
  layers          = [aws_lambda_layer_version.deps.arn]
  memory_mb       = 1024
  timeout_seconds = 60
  alarm_actions   = [aws_sns_topic.alerts.arn]
  environment     = local.common_env
  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"] # UpdateItem: estado de sincronización
        Resource = local.table_arn
      },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = module.work_queue.queue_arn },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:radar-gastos/*"
      },
      { Effect = "Allow", Action = ["ce:GetCostAndUsage"], Resource = "*" },
      { Effect = "Allow", Action = ["sts:AssumeRole"], Resource = "arn:aws:iam::*:role/radar-gastos-cost-reader" },
    ]
  })
}

resource "aws_lambda_event_source_mapping" "collector" {
  event_source_arn                   = module.work_queue.queue_arn
  function_name                      = module.collector_fn.function_arn
  batch_size                         = 10
  maximum_batching_window_in_seconds = 1
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.collector_max_concurrency
  }
}

module "scheduler_fn" {
  source          = "../../modules/lambda_function"
  name            = "${local.name}-scheduler"
  description     = "Fan-out horario de tareas de recolección"
  handler         = "radar_gastos.handlers.scheduler.handler"
  package_path    = var.lambda_package
  layers          = [aws_lambda_layer_version.deps.arn]
  memory_mb       = 256
  timeout_seconds = 30
  alarm_actions   = [aws_sns_topic.alerts.arn]
  environment     = local.common_env
  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:Query"], Resource = local.table_arn },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = module.work_queue.queue_arn },
    ]
  })
}

module "budget_fn" {
  source          = "../../modules/lambda_function"
  name            = "${local.name}-budget"
  description     = "Evaluación diaria de presupuestos y alertas"
  handler         = "radar_gastos.handlers.budget.handler"
  package_path    = var.lambda_package
  layers          = [aws_lambda_layer_version.deps.arn]
  memory_mb       = 512
  timeout_seconds = 120
  alarm_actions   = [aws_sns_topic.alerts.arn]
  environment     = merge(local.common_env, { ALERT_TOPIC_ARN = aws_sns_topic.alerts.arn })
  policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:PutItem"], Resource = local.table_arn },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
    ]
  })
}

# ---------------------------------------------------------------- planificación
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-eventbridge-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [module.scheduler_fn.function_arn, module.budget_fn.function_arn]
    }]
  })
}

resource "aws_scheduler_schedule" "collect" {
  name                = "${local.name}-collect-hourly"
  schedule_expression = "rate(1 hour)"
  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 5
  }
  target {
    arn      = module.scheduler_fn.function_arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 3
    }
  }
}

resource "aws_scheduler_schedule" "budgets" {
  name                         = "${local.name}-budgets-daily"
  schedule_expression          = "cron(0 13 * * ? *)"
  schedule_expression_timezone = "UTC"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = module.budget_fn.function_arn
    role_arn = aws_iam_role.scheduler.arn
  }
}
