# Modelo de seguridad

## Amenazas consideradas (STRIDE resumido)

| Amenaza | Vector | Control |
|---|---|---|
| **S**poofing | Token falsificado | Autorizador JWT de API Gateway valida firma, emisor y audiencia antes de invocar Lambda. |
| **T**ampering | Cliente envía datos para otro tenant | `tenant_id` prohibido en el cuerpo (`extra="forbid"`); se deriva de la claim del token. Prueba: `test_push_ingest_end_to_end`, `test_tenant_isolation`. |
| **R**epudiation | Cambios de gobierno sin rastro | Logs estructurados con `sub` del autor en cada cambio de mapeo; logs de acceso de API Gateway. |
| **I**nformation disclosure | Fuga de credenciales de proveedores | Secrets Manager (KMS), IAM restringido a `secret:radar-gastos/*`, nunca en variables de entorno ni logs. Llaves Admin de **solo lectura** de uso/costos. La API de Configuración es *write-only* para secretos: nunca los devuelve, y los errores de "Probar conexión" se depuran (`safe_error`). En local se guardan en `~/.radar-gastos` (fuera del repositorio, 0600 en POSIX). |
| **I**nformation disclosure | Lectura entre tenants (OWASP API1 BOLA) | Todas las claves de DynamoDB comienzan con `T#{tenant}`; el tenant proviene del token. |
| **D**enial of service | Ráfagas a la API | Throttling por stage (500 rps / 1000 burst), límite de 1000 registros por petición, SQS como *buffer*, concurrencia máxima del consumidor. |
| **E**levation of privilege | Usuario común modifica presupuestos | Grupo Cognito `admins` verificado en cada ruta de escritura; MFA TOTP disponible. |
| Confused deputy | Asumir rol de Cost Explorer ajeno | `ExternalId` + condición `aws:PrincipalArn` en la política de confianza. |

## Controles de plataforma

- Cifrado en reposo con una **CMK propia** (`infra/terraform/envs/dev/kms.tf`, rotación anual): DynamoDB,
  logs de CloudWatch, variables de entorno de Lambda, tópico SNS y horarios de EventBridge Scheduler. SQS usa
  SSE-SQS, Secrets Manager su llave administrada y el bucket del frontend SSE-S3. En tránsito: TLS 1.2+.
- La política de la llave delega en IAM y solo abre el servicio de CloudWatch Logs (limitado a los log groups de
  la cuenta) y las alarmas de CloudWatch (que no pueden publicar en un tópico cifrado con `aws/sns`).
- Logs retenidos **365 días** (funciones y accesos a la API) para auditoría.
- Frontend: S3 privado + CloudFront OAC; CSP estricta (`script-src 'self' cdnjs`), HSTS, `X-Frame-Options: DENY`;
  versiones del bucket para revertir un despliegue (las anteriores se borran a los 30 días).
- Mínimo privilegio: un rol IAM por función con acciones explícitas (ver `infra/terraform/envs/dev/main.tf`).
  EventBridge Scheduler invoca con su rol de ejecución, sin permisos abiertos en las funciones.
- CI: `bandit`, `pip-audit`, `checkov`, `gitleaks` (pre-commit). Despliegue con OIDC (sin llaves estáticas).
  **Checkov bloquea el pipeline**: cada excepción va como `checkov:skip` en el propio recurso, con su motivo.

## Excepciones aceptadas (prototipo)

| Hallazgo | Justificación |
|---|---|
| CORS `*` en la API | Autenticación por *bearer token* (sin cookies); restringir crearía un ciclo CSP↔CORS en Terraform. |
| `ce:GetCostAndUsage` con `Resource: *` | Cost Explorer no admite permisos a nivel de recurso. |
| `GET /v1/health` sin autorizador (CKV_AWS_309) | Sonda de disponibilidad pública a propósito; no devuelve datos. |
| Sin AWS WAF (CKV_AWS_68, CKV2_AWS_47) | Costo fijo; el sitio es estático y la API aplica throttling y JWT. Recomendado en producción. |
| Sin dominio propio (CKV2_AWS_42, CKV_AWS_174) | Con el certificado de `*.cloudfront.net` no se puede fijar TLS 1.2 mínimo; se resuelve con dominio y ACM. |
| CloudFront sin logs, failover ni restricción geográfica (CKV_AWS_86, 310, 374) | Costo y alcance del prototipo; la API registra cada llamada y S3 ofrece 99,99 % de disponibilidad. |
| Bucket del frontend sin logs, replicación, KMS ni eventos (CKV_AWS_18, 144, 145, CKV2_AWS_62) | Solo contiene archivos públicos de la SPA, reproducibles desde Git. |
| Lambdas fuera de VPC (CKV_AWS_117) | Solo usan servicios administrados y APIs públicas; una VPC exigiría NAT (costo fijo) sin reducir riesgo. |
| Lambdas sin DLQ propia (CKV_AWS_116) | La API es síncrona, el colector usa la DLQ de SQS y los horarios reintentan en EventBridge Scheduler. |
| Lambdas sin firma de código (CKV_AWS_272) | El mismo pipeline construye y despliega el paquete; recomendado en producción con AWS Signer. |
