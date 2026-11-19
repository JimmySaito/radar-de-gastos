variable "name" {
  description = "Nombre de la función (también prefijo del rol y del log group)."
  type        = string
}

variable "description" {
  type    = string
  default = ""
}

variable "handler" {
  description = "Ruta del handler, p. ej. radar_gastos.handlers.api.handler"
  type        = string
}

variable "package_path" {
  description = "Zip con el código de la aplicación (generado por make build)."
  type        = string
}

variable "layers" {
  type    = list(string)
  default = []
}

variable "memory_mb" {
  type    = number
  default = 512
}

variable "timeout_seconds" {
  type    = number
  default = 30
}

variable "reserved_concurrency" {
  description = "Concurrencia reservada (-1 = sin reserva). Protege la base de datos y a los proveedores de picos."
  type        = number
  default     = -1
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "policy_json" {
  description = "Política IAM inline de mínimo privilegio."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "Retención de logs. Un año cubre auditorías y cierres contables anuales."
  type        = number
  default     = 365
}

variable "kms_key_arn" {
  description = "CMK para cifrar el log group y las variables de entorno (la política de la llave debe permitir a CloudWatch Logs)."
  type        = string
  default     = null
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "error_alarm_threshold" {
  type    = number
  default = 1
}

variable "alarm_actions" {
  type    = list(string)
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "role_arn" {
  description = "Rol existente para la función. Si es null, el módulo crea uno con policy_json."
  type        = string
  default     = null
}

variable "tracing_mode" {
  description = "X-Ray: Active, o PassThrough donde X-Ray no está disponible."
  type        = string
  default     = "Active"
  validation {
    condition     = contains(["Active", "PassThrough"], var.tracing_mode)
    error_message = "tracing_mode debe ser Active o PassThrough."
  }
}
