#
# Llave KMS administrada por el cliente (CMK) para los datos en reposo: tabla de
# DynamoDB, logs de CloudWatch, tópico SNS de alertas y horarios de EventBridge
# Scheduler. Da rotación anual, auditoría en CloudTrail y la posibilidad de
# revocar el acceso, cosa que las llaves administradas por AWS no permiten.
#
# También es necesaria para que las alarmas funcionen: CloudWatch no puede
# publicar en un tópico SNS cifrado con la llave aws/sns.
#

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}

resource "aws_kms_key" "data" {
  count                   = var.enable_cmk ? 1 : 0
  description             = "${local.name}: cifrado de datos en reposo"
  enable_key_rotation     = true
  rotation_period_in_days = 365
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Delega en IAM: los roles de la cuenta usan la llave solo si su política lo permite.
        Sid       = "AdministracionPorLaCuenta"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "LogsDeCloudWatch"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${local.region}:${local.account_id}:log-group:*"
          }
        }
      },
      {
        # Las alarmas publican en el tópico SNS cifrado.
        Sid       = "AlarmasDeCloudWatch"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "data" {
  count         = var.enable_cmk ? 1 : 0
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.data[0].key_id
}

locals {
  # null: los recursos usan la llave administrada por AWS (o ninguna, en SNS).
  kms_key_arn = var.enable_cmk ? aws_kms_key.data[0].arn : null

  # Permiso que necesita cada función para leer y escribir la tabla y el tópico cifrados.
  kms_use_statements = var.enable_cmk ? [{
    Effect   = "Allow"
    Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    Resource = aws_kms_key.data[0].arn
  }] : []
  kms_decrypt_statements = var.enable_cmk ? [{
    Effect   = "Allow"
    Action   = ["kms:Decrypt"]
    Resource = aws_kms_key.data[0].arn
  }] : []
}
