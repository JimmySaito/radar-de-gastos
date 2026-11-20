# ---------------------------------------------------------------- API HTTP
resource "aws_apigatewayv2_api" "http" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"

  # "*" es seguro aquí: la API se autentica con bearer token (no cookies), y fijar
  # el dominio de CloudFront crearía un ciclo de dependencias (CSP ↔ CORS).
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 600
  }
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.http.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.main.id}"
    audience = [aws_cognito_user_pool_client.spa.id, aws_cognito_user_pool_client.ingest.id]
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.api_fn.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

locals {
  protected_routes = [
    "GET /v1/me",
    "GET /v1/summary",
    "GET /v1/timeseries",
    "GET /v1/identities",
    "POST /v1/usage/records",
    "GET /v1/providers",
    "GET /v1/connectors",
    "PUT /v1/connectors",
    "POST /v1/connectors/test",
    "POST /v1/connectors/{connector_id}/sync",
    "DELETE /v1/connectors/{connector_id}",
    "GET /v1/mappings",
    "PUT /v1/mappings",
    "DELETE /v1/mappings",
    "GET /v1/budgets",
    "PUT /v1/budgets",
    "DELETE /v1/budgets",
    "GET /v1/recurring/{scope}/catalog",
    "GET /v1/recurring/{scope}",
    "PUT /v1/recurring/{scope}",
    "DELETE /v1/recurring/{scope}/{expense_id}",
    "GET /v1/personal/profiles",
    "PUT /v1/personal/profiles",
    "DELETE /v1/personal/profiles/{profile_id}",
  ]
}

resource "aws_apigatewayv2_route" "protected" {
  for_each           = toset(local.protected_routes)
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

resource "aws_apigatewayv2_route" "health" {
  # checkov:skip=CKV_AWS_309: sonda de disponibilidad pública a propósito; no devuelve datos
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /v1/health"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

# Chequeo profundo: verifica que la API pueda leer DynamoDB (503 si no).
resource "aws_apigatewayv2_route" "ready" {
  # checkov:skip=CKV_AWS_309: sonda de disponibilidad pública a propósito; solo devuelve ok/degradado
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /v1/ready"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

# Sin CloudFront, la API entrega el dashboard: "/" y cualquier ruta fuera de /v1 (las rutas
# /v1 definidas arriba tienen prioridad sobre {proxy+}).
resource "aws_apigatewayv2_route" "web" {
  for_each = local.web_on_api ? toset(["GET /", "GET /{proxy+}"]) : toset([])
  # checkov:skip=CKV_AWS_309: archivos públicos del dashboard; los datos siguen protegidos por JWT en /v1
  api_id    = aws_apigatewayv2_api.http.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${local.name}"
  retention_in_days = var.log_retention_days # auditoría: quién llamó qué ruta
  kms_key_id        = local.kms_key_arn
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit    = var.api_throttle_rate
    throttling_burst_limit   = var.api_throttle_burst
    detailed_metrics_enabled = true
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      latency        = "$context.responseLatency"
      integrationErr = "$context.integrationErrorMessage"
      sub            = "$context.authorizer.claims.sub"
    })
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = module.api_fn.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

