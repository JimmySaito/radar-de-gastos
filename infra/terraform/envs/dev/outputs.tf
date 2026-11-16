output "api_url" {
  value = aws_apigatewayv2_api.http.api_endpoint
}

output "dashboard_url" {
  value = module.frontend.url
}

output "cognito_domain" {
  value = local.cognito_domain_url
}

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "spa_client_id" {
  value = aws_cognito_user_pool_client.spa.id
}

output "ingest_client_id" {
  value = aws_cognito_user_pool_client.ingest.id
}

output "ingest_client_secret" {
  value     = aws_cognito_user_pool_client.ingest.client_secret
  sensitive = true
}

output "table_name" {
  value = aws_dynamodb_table.main.name
}

output "work_queue_url" {
  value = module.work_queue.queue_url
}

output "ops_dashboard" {
  value = "https://${data.aws_region.current.name}.console.aws.amazon.com/cloudwatch/home#dashboards/dashboard/${aws_cloudwatch_dashboard.ops.dashboard_name}"
}
