# Runbook operativo

## 1. Despliegue inicial

En Windows basta con `.\scripts\deploy_aws.ps1 -AdminEmail tu@correo.com` (incluye los pasos 2 y 3); ver [despliegue.md](despliegue.md). A mano:

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python scripts/build_lambda.py                   # dist/app.zip + dist/layer.zip (arm64)
cd infra/terraform/envs/dev
cp terraform.tfvars.example terraform.tfvars     # ajustar cognito_domain_prefix y alert_email
cp backend.tf.example backend.tf                 # opcional: estado remoto en S3
terraform init && terraform apply
```

## 2. Alta del primer administrador

```bash
POOL=$(terraform output -raw user_pool_id)
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username admin@empresa.com \
  --user-attributes Name=email,Value=admin@empresa.com Name=email_verified,Value=true Name=custom:tenant_id,Value=acme
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username admin@empresa.com --group-name admins
```

## 3. Cargar la vista de ejemplo

```bash
python scripts/seed_demo.py --table "$(terraform output -raw table_name)" --region us-east-1
```

Registra el conector sintético del tenant `demo` (el scheduler lo mantiene al día), sus mapeos y presupuestos.

## 4. Registrar conectores

**Desde el dashboard (recomendado):** *Datos reales → Configuración → Conexiones*. La API guarda la credencial
en Secrets Manager (`radar-gastos/{tenant}/{conexión}`) y la configuración solo conserva el ARN.

**Por API (automatización):** la misma operación que usa la pantalla.

```bash
curl -X PUT "$API/v1/connectors" -H "Authorization: Bearer $ID_TOKEN_ADMIN" -H "content-type: application/json"   -d '{"provider":"anthropic","label":"Claude producción","values":{"admin_api_key":"sk-ant-admin..."}}'
curl -X POST "$API/v1/connectors/<connector_id>/sync?days=30" -H "Authorization: Bearer $ID_TOKEN_ADMIN"
```

| Proveedor | `values` |
|---|---|
| Anthropic | `{"admin_api_key": "sk-ant-admin..."}` |
| OpenAI | `{"admin_api_key": "sk-admin-..."}` |
| AWS | `{"role_arn": "arn:aws:iam::<payer>:role/radar-gastos-cost-reader", "external_id": "...", "owner_tag": "owner"}` |
| Google | `{"service_account_json": "{...}", "billing_table": "proyecto.dataset.tabla"}` |

Después, mapear identidades técnicas a personas y equipos (*Configuración → Personas y equipos* o `PUT /v1/mappings`).

## 5. Respuesta a alarmas

| Alarma | Significado | Acción |
|---|---|---|
| `*-work-dlq-not-empty` | Mensajes fallaron 5 veces | Revisar logs del colector (`/aws/lambda/radar-gastos-dev-collector`) por `record_id`/`connector`. Corregir causa (credencial vencida, cambio de API del proveedor) y hacer *redrive* desde la consola de SQS. El ledger es idempotente: reprocesar es seguro. |
| `*-work-backlog-age` | Cola con retraso > 15 min | Ver `ConcurrentExecutions` y `LedgerRetryTransactionConflict`. Si hay muchos conflictos, el cuello es una celda agregada caliente: no subir concurrencia, ver ADR-0004 (sharding de agregados). Si hay throttling de un proveedor, bajar `collector_max_concurrency`. |
| `*-api-5xx` | > 1 % de errores | Revisar X-Ray (traza por `requestId`) y logs JSON de `radar-gastos-dev-api`. |
| `*-api-latency-p95` | p95 > 800 ms | Revisar tamaño de rangos consultados; considerar caché (ADR-0007, trabajo futuro). |
| `*-<función>-errors` | Excepciones no controladas | Logs con `correlation_id`. |

## 6. Rotación de credenciales

Desde Configuración → Editar, pegar la nueva llave (los campos vacíos conservan la anterior), o actualizar el secreto directamente. Los Lambdas lo recargan en ≤ 5 min (caché TTL). No requiere redespliegue.

## 7. Recuperación ante desastres

- **RPO** ≤ 5 min: DynamoDB PITR (35 días). **RTO** ≈ 30 min: `terraform apply` en otra región + restauración PITR.
- Los hechos son reconstruibles desde los proveedores (≤ 365 días en Anthropic/OpenAI): ejecutar el scheduler con `lookback_days` amplio.
