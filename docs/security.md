# Modelo de seguridad

## Amenazas consideradas (STRIDE resumido)

| Amenaza | Vector | Control |
|---|---|---|
| **S**poofing | Token falsificado | Autorizador JWT de API Gateway valida firma, emisor y audiencia antes de invocar Lambda. |
| **T**ampering | Cliente envía datos para otro tenant | `tenant_id` prohibido en el cuerpo (`extra="forbid"`); se deriva de la claim del token. Prueba: `test_push_ingest_end_to_end`, `test_tenant_isolation`. |
| **R**epudiation | Cambios de gobierno sin rastro | Logs estructurados con `sub` del autor en cada cambio de mapeo; logs de acceso de API Gateway. |
| **I**nformation disclosure | Fuga de credenciales de proveedores | Secrets Manager (KMS), IAM restringido a `secret:usagehub/*`, nunca en variables de entorno ni logs. Llaves Admin de **solo lectura** de uso/costos. La API de Configuración es *write-only* para secretos: nunca los devuelve, y los errores de "Probar conexión" se depuran (`safe_error`). En local se guardan en `~/.usagehub` (fuera del repositorio, 0600 en POSIX). |
| **I**nformation disclosure | Lectura entre tenants (OWASP API1 BOLA) | Todas las claves de DynamoDB comienzan con `T#{tenant}`; el tenant proviene del token. |
| **D**enial of service | Ráfagas a la API | Throttling por stage (500 rps / 1000 burst), límite de 1000 registros por petición, SQS como *buffer*, concurrencia máxima del consumidor. |
| **E**levation of privilege | Usuario común modifica presupuestos | Grupo Cognito `admins` verificado en cada ruta de escritura; MFA TOTP disponible. |
| Confused deputy | Asumir rol de Cost Explorer ajeno | `ExternalId` + condición `aws:PrincipalArn` en la política de confianza. |

## Controles de plataforma

- Cifrado en reposo: DynamoDB (KMS AWS-owned), SQS (SSE-SQS), SNS (KMS), S3 (SSE-S3); en tránsito: TLS 1.2+.
- Frontend: S3 privado + CloudFront OAC; CSP estricta (`script-src 'self' cdnjs`), HSTS, `X-Frame-Options: DENY`.
- Mínimo privilegio: un rol IAM por función con acciones explícitas (ver `infra/terraform/envs/dev/main.tf`).
- CI: `bandit`, `pip-audit`, `checkov`, `gitleaks` (pre-commit). Despliegue con OIDC (sin llaves estáticas).

## Excepciones aceptadas (prototipo)

| Hallazgo | Justificación |
|---|---|
| CORS `*` en la API | Autenticación por *bearer token* (sin cookies); restringir crearía un ciclo CSP↔CORS en Terraform. |
| Sin AWS WAF | Costo fijo; el throttling de API Gateway cubre el alcance del prototipo. Recomendado en producción. |
| KMS administrado por AWS (no CMK) | Suficiente para el prototipo; CMK recomendado para cumplimiento (rotación y auditoría propias). |
| `ce:GetCostAndUsage` con `Resource: *` | Cost Explorer no admite permisos a nivel de recurso. |
