#
# Se despliega en la cuenta de administración (payer) de AWS Organizations.
# Permite que UsageHub lea Cost Explorer con ExternalId (mitiga "confused deputy").
#

variable "usagehub_account_id" {
  description = "Cuenta donde corre UsageHub (la que asume este rol)."
  type        = string
}

variable "external_id" {
  description = "Secreto compartido; igual al campo external_id del secreto del conector AWS."
  type        = string
  sensitive   = true
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.usagehub_account_id}:root"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.external_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${var.usagehub_account_id}:role/usagehub-*-collector-role"]
    }
  }
}

resource "aws_iam_role" "reader" {
  name                 = "usagehub-cost-reader"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "ce_read" {
  role = aws_iam_role.reader.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["ce:GetCostAndUsage", "ce:GetTags"], Resource = "*" }]
  })
}

output "role_arn" {
  value = aws_iam_role.reader.arn
}
