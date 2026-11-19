locals {
  cognito_domain_url = "https://${var.cognito_domain_prefix}.auth.${data.aws_region.current.name}.amazoncognito.com"
  web_on_api         = var.frontend_hosting == "api"
  dashboard_url      = local.web_on_api ? aws_apigatewayv2_api.http.api_endpoint : module.frontend[0].url
}

module "frontend" {
  count      = local.web_on_api ? 0 : 1
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
