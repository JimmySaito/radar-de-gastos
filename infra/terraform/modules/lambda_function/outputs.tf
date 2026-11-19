output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "function_arn" {
  value = aws_lambda_function.this.arn
}

output "invoke_arn" {
  value = aws_lambda_function.this.invoke_arn
}

output "role_name" {
  description = "Rol creado por el módulo; null si se usa un rol existente (role_arn)."
  value       = local.create_role ? aws_iam_role.this[0].name : null
}

output "role_arn" {
  value = local.role_arn
}
