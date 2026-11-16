locals {
  cognito_domain_url = "https://${var.cognito_domain_prefix}.auth.${data.aws_region.current.name}.amazoncognito.com"
}

module "frontend" {
  source     = "../../modules/static_site"
  name       = "${local.name}-web"
  source_dir = "${path.module}/../../../../frontend"
  connect_src = [
    aws_apigatewayv2_api.http.api_endpoint,
    local.cognito_domain_url,
  ]
  force_destroy = !var.enable_deletion_protection
  runtime_config = {
    apiBaseUrl    = aws_apigatewayv2_api.http.api_endpoint
    authMode      = "cognito"
    cognitoDomain = local.cognito_domain_url
    clientId      = aws_cognito_user_pool_client.spa.id
  }
}
