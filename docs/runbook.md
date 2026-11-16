# Runbook operativo

## 1. Despliegue inicial

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

## 3. Registrar conectores

1. Guardar la credencial **de solo lectura** en Secrets Manager (el prefijo `usagehub/` es obligatorio por IAM):

   | Proveedor | Secreto (JSON) |
   |---|---|
   | Anthropic | `{"admin_api_key": "sk-ant-admin..."}` |
   | OpenAI | `{"admin_api_key": "sk-admin-..."}` |
   | AWS | `{"role_arn": "arn:aws:iam::<payer>:role/usagehub-cost-reader", "external_id": "..."}` |
   | Google | `{"service_account_json": "{...}"}` + opción `billing_table` |

   ```bash
   aws secretsmanager create-secret --name usagehub/acme/anthropic --secret-string '{"admin_api_key":"..."}'
   ```

2. Registrar el conector vía API (`PUT /v1/connectors`) con el ARN del secreto.
3. Mapear identidades técnicas a personas/equipos (`PUT /v1/mappings`).

## 4. Respuesta a alarmas

| Alarma | Significado | Acción |
|---|---|---|
| `*-work-dlq-not-empty` | Mensajes fallaron 5 veces | Revisar logs del colector (`/aws/lambda/usagehub-dev-collector`) por `record_id`/`connector`. Corregir causa (credencial vencida, cambio de API del proveedor) y hacer *redrive* desde la consola de SQS. El ledger es idempotente: reprocesar es seguro. |
| `*-work-backlog-age` | Cola con retraso > 15 min | Ver `ConcurrentExecutions` y `LedgerRetryTransactionConflict`. Si hay muchos conflictos, el cuello es una celda agregada caliente: no subir concurrencia, ver ADR-0004 (sharding de agregados). Si hay throttling de un proveedor, bajar `collector_max_concurrency`. |
| `*-api-5xx` | > 1 % de errores | Revisar X-Ray (traza por `requestId`) y logs JSON de `usagehub-dev-api`. |
| `*-api-latency-p95` | p95 > 800 ms | Revisar tamaño de rangos consultados; considerar caché (ADR-0007, trabajo futuro). |
| `*-<función>-errors` | Excepciones no controladas | Logs con `correlation_id`. |

## 5. Rotación de credenciales

Actualizar el valor del secreto; los Lambdas lo recargan en ≤ 5 min (caché TTL). No requiere redespliegue.

## 6. Recuperación ante desastres

- **RPO** ≤ 5 min: DynamoDB PITR (35 días). **RTO** ≈ 30 min: `terraform apply` en otra región + restauración PITR.
- Los hechos son reconstruibles desde los proveedores (≤ 365 días en Anthropic/OpenAI): ejecutar el scheduler con `lookback_days` amplio.
