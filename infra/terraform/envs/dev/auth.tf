# ---------------------------------------------------------------- identidad
# Cognito: usuarios humanos (SPA con Authorization Code + PKCE) y clientes
# máquina-a-máquina (client_credentials con scope usagehub/ingest).

resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  mfa_configuration        = "OPTIONAL"
  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]
  deletion_protection      = var.enable_deletion_protection ? "ACTIVE" : "INACTIVE"

  admin_create_user_config {
    allow_admin_create_user_only = true # alta controlada por el administrador del tenant
  }

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  schema {
    name                     = "tenant_id"
    attribute_data_type      = "String"
    mutable                  = false # un usuario no puede cambiarse de tenant
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 2
      max_length = 63
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_group" "admins" {
  name         = "admins"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Administradores FinOps del tenant: gestionan conectores, mapeos y presupuestos."
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_resource_server" "api" {
  identifier   = "usagehub"
  name         = "UsageHub API"
  user_pool_id = aws_cognito_user_pool.main.id

  scope {
    scope_name        = "ingest"
    scope_description = "Enviar registros de consumo (push)"
  }
}

resource "aws_cognito_user_pool_client" "spa" {
  name                                 = "${local.name}-spa"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${module.frontend.url}/", "http://localhost:8080/"]
  logout_urls                          = ["${module.frontend.url}/", "http://localhost:8080/"]
  read_attributes                      = ["email", "custom:tenant_id"]
  write_attributes                     = ["email"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  id_token_validity                    = 60
  access_token_validity                = 60
  refresh_token_validity               = 8
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "hours"
  }
}

resource "aws_cognito_user_pool_client" "ingest" {
  name                                 = "${local.name}-ingest-m2m"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = ["usagehub/ingest"]
  supported_identity_providers         = ["COGNITO"]
  access_token_validity                = 60
  token_validity_units {
    access_token = "minutes"
  }
  depends_on = [aws_cognito_resource_server.api]
}
