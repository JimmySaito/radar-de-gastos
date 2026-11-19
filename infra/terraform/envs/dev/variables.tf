variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "cost_center" {
  type    = string
  default = "finops"
}

variable "alert_email" {
  description = "Correo que recibe alertas de presupuesto y operación (requiere confirmar la suscripción)."
  type        = string
  default     = ""
}

variable "cognito_domain_prefix" {
  description = "Prefijo único global para el Hosted UI de Cognito."
  type        = string
}

variable "collector_max_concurrency" {
  description = "Concurrencia máxima del consumidor SQS: limita conflictos transaccionales y protege APIs de proveedores."
  type        = number
  default     = 20
}

variable "api_throttle_rate" {
  description = "Solicitudes/segundo sostenidas permitidas por la API (stage default)."
  type        = number
  default     = 500
}

variable "api_throttle_burst" {
  type    = number
  default = 1000
}

variable "lambda_package" {
  type    = string
  default = "../../../../dist/app.zip"
}

variable "lambda_layer_package" {
  type    = string
  default = "../../../../dist/layer.zip"
}

variable "m2m_tenant" {
  description = "Tenant al que pertenece el cliente máquina-a-máquina de ingesta creado en este entorno."
  type        = string
  default     = "demo"
}

variable "enable_deletion_protection" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  description = "Retención de los logs de acceso de la API (días)."
  type        = number
  default     = 365
}

# ---------------------------------------------------------------- cuentas restringidas
# AWS Academy Learner Lab no permite crear roles IAM ni usar CloudFront o X-Ray.
# scripts/deploy_aws.ps1 detecta el laboratorio y fija estas tres variables.

variable "lab_role_arn" {
  description = "Rol existente (LabRole) para Lambdas y Scheduler. Vacío: se crean roles de mínimo privilegio."
  type        = string
  default     = ""
}

variable "enable_cmk" {
  description = "Cifrar con una llave KMS propia. Si es false se usan las llaves administradas por AWS."
  type        = bool
  default     = true
}

variable "frontend_hosting" {
  description = "Dónde se sirve el dashboard: cloudfront (S3 + CloudFront) o api (la propia API, sin CloudFront)."
  type        = string
  default     = "cloudfront"
  validation {
    condition     = contains(["cloudfront", "api"], var.frontend_hosting)
    error_message = "frontend_hosting debe ser cloudfront o api."
  }
}
