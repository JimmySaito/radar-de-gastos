# Despliegue: local y AWS

Radar de gastos corre de dos formas con el mismo código. Conviene tener las dos: AWS es el prototipo que pide el
curso y local es el respaldo si algo falla al presentar.

| | Local | AWS |
|---|---|---|
| Para qué | Desarrollo y **respaldo** de la presentación | Prototipo real, pruebas de estrés |
| Cómo se abre | `http://localhost:8080` | URL de CloudFront (`https://xxxx.cloudfront.net`) |
| Inicio de sesión | No hace falta | Cognito (correo y contraseña, MFA opcional) |
| Dónde guarda los datos | `~/.radar-gastos` en tu equipo | DynamoDB y Secrets Manager en tu cuenta |
| Necesita internet | Solo para sincronizar proveedores | Sí |
| Costo | Ninguno | Pocos dólares al mes sin carga (ver abajo) |

Los dos ambientes **no comparten datos**: las conexiones, suscripciones y personas que configures en uno no aparecen
en el otro. La vista *Ejemplo* está en ambos.

## 1. Local

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python scripts\local_server.py
```

Abre `http://localhost:8080`. Las gráficas usan una copia de Chart.js incluida en `frontend/vendor`, así que la página
funciona aunque no haya internet (solo fallará sincronizar con los proveedores).

## 2. AWS

### Requisitos (una sola vez)

1. **Terraform**: `winget install Hashicorp.Terraform` y abre una terminal nueva (`terraform -version`).
2. **AWS CLI** (ya instalado): `aws --version`.
3. **Sesión de AWS** con un usuario que pueda crear la infraestructura (no uses el usuario raíz):
   - con usuario y contraseña de la consola: `aws login`;
   - con IAM Identity Center (SSO): `aws configure sso` y luego `aws sso login`.

   Comprueba la sesión con `aws sts get-caller-identity`.
4. Recomendado: en *Billing → Budgets* crea un presupuesto mensual (por ejemplo 10 USD) con aviso por correo.

### Desplegar

Desde la carpeta del repositorio:

```powershell
.\scripts\deploy_aws.ps1 -AdminEmail tu@correo.com -Tenant mi-empresa
```

El script empaqueta las Lambdas, crea `terraform.tfvars` la primera vez, muestra el plan de Terraform y pide
confirmación (`yes`), carga la vista *Ejemplo* y te da de alta como administrador. Al terminar imprime la URL del
dashboard. Si PowerShell bloquea el script, permite scripts locales solo en esa terminal:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Después:

1. Revisa tu correo: Cognito envía una **contraseña temporal** (válida 3 días) y SNS pide **confirmar la
   suscripción** a las alertas. **Confírmala**: sin ese paso las alarmas se disparan pero el correo nunca llega (la
   suscripción queda en `PendingConfirmation`).
2. Abre la URL del dashboard, inicia sesión y cambia la contraseña. La primera vez CloudFront puede tardar unos
   minutos.
3. En *Datos reales → ⚙ Configuración* conecta tus proveedores igual que en local; las llaves quedan en
   Secrets Manager.

Volver a ejecutar el script es seguro: aplica solo los cambios y no duplica usuarios ni datos de ejemplo.

### Costo y limpieza

Sin carga, el costo lo marcan los cargos fijos: la llave KMS (1 USD/mes), cada secreto de Secrets Manager
(0,40 USD/mes) y las alarmas y el dashboard de CloudWatch si superas la capa gratuita. Lambda, API Gateway,
DynamoDB y CloudFront cuestan centavos con el uso de una demo. Las pruebas de estrés sí generan costo por
solicitud: revisa *Billing* después de ejecutarlas.

Para borrar todo:

```powershell
.\scripts\deploy_aws.ps1 -Destroy
```

El estado de Terraform se guarda en `infra/terraform/envs/dev/terraform.tfstate`: **no lo borres** mientras exista
el despliegue, porque es lo que permite actualizarlo o eliminarlo. Para guardarlo en S3, ver
`backend.tf.example`.

### AWS Academy Learner Lab

El laboratorio no permite crear roles IAM ni usar CloudFront o X-Ray. El script lo detecta (rol `voclabs`) y
activa solo el **modo laboratorio**, que cambia esto:

| | Despliegue normal | Learner Lab |
|---|---|---|
| Roles de las funciones y del Scheduler | Uno por función, de mínimo privilegio | `LabRole`, el rol del laboratorio |
| Dashboard | S3 + CloudFront | La propia API (HTTPS de API Gateway) |
| Cifrado | Llave KMS propia | Llaves administradas por AWS |
| Trazas X-Ray | Activas | Desactivadas |
| Concurrencia de Lambda | Sin techo por función; colector hasta 20 | **Máximo 9 en total** (API 4, colector 3, horarios 1 + 1) |

El resto (Lambda, SQS, DynamoDB, Cognito, Scheduler, alarmas) es idéntico.

> **Importante:** Learner Lab permite como máximo **10 entornos de ejecución de Lambda concurrentes** en toda la
> cuenta y **desactiva la cuenta** si se superan, con pérdida de todos los recursos. Así se perdió el primer
> laboratorio de este proyecto durante una prueba de estrés (la API llegó a 64 ejecuciones simultáneas). Por eso el
> modo laboratorio reserva concurrencia por función (9 en total): bajo carga, el exceso se rechaza con errores
> 429/500 en lugar de superar el límite. No ejecutes pruebas de estrés grandes en el laboratorio;
> `scripts/stress_aws.py` se niega a correr ahí sin `--allow-lab`.

Pasos:

1. En AWS Academy: **Modules → Learner Lab → Start Lab** y espera el punto verde.
2. **AWS Details → AWS CLI: Show**: copia el bloque y pégalo en `notepad $HOME\.aws\credentials`. Al guardar,
   el archivo debe llamarse `credentials`, **sin** `.txt` (en el cuadro de guardar elige *Todos los archivos*).
3. `aws configure set region us-east-1` y comprueba con `aws sts get-caller-identity` (debe decir `voclabs`).
4. `.\scripts\deploy_aws.ps1 -AdminEmail tu@correo.com -Tenant mi-empresa`.

Las credenciales cambian en cada sesión del laboratorio: repite el paso 2 cada vez. Al terminar la sesión los
recursos y los datos se conservan; el dashboard y el inicio de sesión siguen en línea, pero la API no puede
leer la base de datos y responde 503 (medido en la prueba de disponibilidad). Con *Start Lab* vuelve a funcionar
sin redesplegar. Para usar solo el dashboard no hace falta copiar credenciales: basta con *Start Lab*.

## 3. Estado actual (noviembre de 2026)

- El laboratorio donde se desplegó y midió el prototipo fue **desactivado** por superar el límite de concurrencia de
  Lambda durante una prueba de estrés (ver arriba). Los resultados medidos allí están en `docs/results/aws-*.json`.
- El segundo laboratorio disponible **no permite** crear Lambda, DynamoDB, SQS, API Gateway, Cognito ni roles IAM
  (solo EC2 y S3), así que la arquitectura serverless no se puede desplegar en él.
- **La demostración se hace en local**, con el mismo código y las mismas políticas de seguridad del navegador (CSP)
  que en AWS. Si se obtiene un Learner Lab nuevo: borrar `terraform.tfstate*` y `terraform.tfvars`, desplegar con el
  script (aplica el límite de 9 Lambdas) y restaurar la configuración con `scripts/migrate_local_to_aws.py --apply --sync`.

## 4. Presentación en local

**El día anterior**

- [ ] `.venv\Scripts\python -m pytest -q`: todo en verde.
- [ ] Arrancar el servidor local y revisar *Ejemplo*, *Datos reales*, *Personal* y *Configuración*.
- [ ] Sincronizar las conexiones desde Configuración para que los datos reales estén al día.

**Antes de empezar**

- [ ] `.venv\Scripts\python scripts\local_server.py` y abrir `http://localhost:8080`.
- [ ] Tener a mano los resultados medidos en AWS (`docs/results/`) y la sección 8.4 del repositorio de arquitectura
      para mostrar la evidencia de la nube.

**Si falla internet:** la vista *Ejemplo*, el espacio *Personal* y lo ya sincronizado siguen funcionando; solo falla
sincronizar con los proveedores.

## 5. Plan de respaldo con AWS disponible

**El día anterior**

- [ ] Ejecuta `.\scripts\deploy_aws.ps1` para confirmar que el despliegue está al día e inicia sesión en el dashboard.
- [ ] Ejecuta `.venv\Scripts\python -m pytest -q` y arranca el servidor local una vez.
- [ ] Revisa que la vista *Ejemplo* y tus *Datos reales* se ven bien en los dos ambientes.

**Antes de empezar**

- [ ] Abre la pestaña de AWS con la sesión ya iniciada (el token dura 1 hora; recarga si caduca).
- [ ] Arranca el servidor local y deja `http://localhost:8080` abierto en otra pestaña.
- [ ] Ten a mano el dashboard de operación de CloudWatch (`terraform output ops_dashboard`) para mostrar métricas.

**Si AWS falla**: cambia a la pestaña local; la interfaz es la misma. **Si falla internet**: la vista *Ejemplo* y lo ya
sincronizado en local siguen funcionando.
