#
# Módulo reutilizable: cola SQS con DLQ, cifrado y alarmas de confiabilidad.
#

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-dlq"
  message_retention_seconds = 1209600 # 14 días para análisis y redrive
  sqs_managed_sse_enabled   = true
  tags                      = var.tags
}

resource "aws_sqs_queue" "this" {
  name                       = var.name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.retention_seconds
  receive_wait_time_seconds  = 20 # long polling
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
  tags = var.tags
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this.arn]
  })
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name}-dlq-not-empty"
  alarm_description   = "Hay mensajes en la DLQ: requieren análisis y redrive."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "oldest_message" {
  alarm_name          = "${var.name}-backlog-age"
  alarm_description   = "El mensaje más antiguo supera el objetivo de frescura."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.this.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.max_age_alarm_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  tags                = var.tags
}
