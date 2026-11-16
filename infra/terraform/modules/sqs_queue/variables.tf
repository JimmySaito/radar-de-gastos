variable "name" {
  type = string
}

variable "visibility_timeout_seconds" {
  description = "Debe ser >= 6x el timeout del consumidor (recomendación de AWS)."
  type        = number
  default     = 360
}

variable "retention_seconds" {
  type    = number
  default = 345600 # 4 días
}

variable "max_receive_count" {
  type    = number
  default = 5
}

variable "max_age_alarm_seconds" {
  type    = number
  default = 900
}

variable "alarm_actions" {
  type    = list(string)
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
