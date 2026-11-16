variable "name" {
  type = string
}

variable "source_dir" {
  description = "Directorio con los archivos estáticos del frontend."
  type        = string
}

variable "runtime_config" {
  description = "Contenido de /config.json que lee la SPA (URL de la API, Cognito...)."
  type        = map(string)
}

variable "connect_src" {
  description = "Orígenes adicionales permitidos por la CSP (API y dominio de Cognito)."
  type        = list(string)
  default     = []
}

variable "force_destroy" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
