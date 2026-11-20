"""Copia tu configuración local (~/.radar-gastos) a tu despliegue en AWS.

Copia las conexiones (sus credenciales pasan a Secrets Manager), los mapeos, los presupuestos,
las licencias de la empresa y tus suscripciones y personas. El destino es tu usuario de Cognito:
su tenant para lo de empresa y su ``sub`` para lo personal, como si lo hubieras creado en el
dashboard de AWS. No toca la vista de Ejemplo ni tus datos locales.

Por defecto es un simulacro: muestra qué copiaría sin escribir nada. Con --apply escribe; se
puede repetir sin duplicar. Con --sync, además, pide sincronizar los últimos 30 días de cada
conexión copiada. Nunca imprime credenciales.

    python scripts/migrate_local_to_aws.py --email tu@correo.com
    python scripts/migrate_local_to_aws.py --email tu@correo.com --apply --sync

Los nombres de la tabla, el user pool y la cola se leen de las salidas de Terraform.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import boto3

from radar_gastos.adapters.aws import SecretsManagerProvider, SqsWorkQueue
from radar_gastos.adapters.dynamodb import (
    DynamoConfigRepository,
    DynamoMappingRepository,
    DynamoRecurringRepository,
)
from radar_gastos.adapters.localfs import (
    FileConfigRepository,
    FileMappingRepository,
    FileRecurringRepository,
    FileSecretStore,
    default_home,
)
from radar_gastos.services.connections import ConnectionService
from radar_gastos.services.migration import MigrationReport, Store, migrate

TF_DIR = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "envs" / "dev"
LOCAL_TENANT = "local"  # tenant y sub con los que trabaja el servidor local
LOCAL_SUBJECT = "local"


def terraform_outputs() -> dict[str, str]:
    raw = subprocess.run(
        ["terraform", "output", "-json"], cwd=TF_DIR, capture_output=True, text=True, check=True
    ).stdout
    return {k: v["value"] for k, v in json.loads(raw).items() if not v.get("sensitive")}


def cognito_identity(client: object, pool_id: str, email: str) -> tuple[str, str]:
    """Devuelve (tenant, sub) del usuario: lo mismo que la API obtiene de su token."""
    user = client.admin_get_user(UserPoolId=pool_id, Username=email)  # type: ignore[attr-defined]
    attrs = {a["Name"]: a["Value"] for a in user["UserAttributes"]}
    return attrs["custom:tenant_id"], attrs["sub"]


def print_report(report: MigrationReport, *, applied: bool, tenant: str, email: str) -> None:
    verb = "Copiado" if applied else "Se copiaría"
    print(f"\n{verb} a AWS (tenant '{tenant}', usuario {email}):")
    print(f"  Conexiones:              {len(report.connectors)}  {', '.join(report.connectors)}")
    print(f"  Personas:                {report.profiles}")
    print(f"  Suscripciones personales: {report.personal_expenses}")
    print(f"  Licencias de la empresa: {report.business_expenses}")
    print(f"  Mapeos de identidades:   {report.mappings}")
    print(f"  Presupuestos:            {report.budgets}")
    for label, reason in report.skipped:
        print(f"  Omitida: {label}: {reason}")
    if not applied:
        print("\nSimulacro: no se escribió nada. Repite con --apply para copiar.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--email", required=True, help="tu usuario de Cognito en AWS")
    parser.add_argument("--apply", action="store_true", help="escribe (sin esto, simulacro)")
    parser.add_argument("--sync", action="store_true", help="sincroniza las conexiones copiadas")
    parser.add_argument(
        "--home", type=Path, default=None, help="carpeta local (por defecto la del servidor local)"
    )
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    outputs = terraform_outputs()
    session = boto3.Session(region_name=args.region)
    tenant, subject = cognito_identity(
        session.client("cognito-idp"), outputs["user_pool_id"], args.email
    )

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", tenant):
        sys.exit(
            f"El tenant de {args.email} es '{tenant}', y debe ir en minúsculas, números y guiones. "
            "Vuelve a crear el usuario con un tenant válido antes de migrar."
        )

    home = args.home or default_home()
    source = Store(
        configs=FileConfigRepository(home),
        mappings=FileMappingRepository(home),
        secrets=FileSecretStore(home),
        recurring=FileRecurringRepository(home),
    )
    ddb = session.client("dynamodb")
    table = outputs["table_name"]
    target = Store(
        configs=DynamoConfigRepository(ddb, table),
        mappings=DynamoMappingRepository(ddb, table),
        secrets=SecretsManagerProvider(session.client("secretsmanager")),
        recurring=DynamoRecurringRepository(ddb, table),
    )

    report = migrate(
        source,
        target,
        source_tenant=LOCAL_TENANT,
        source_subject=LOCAL_SUBJECT,
        target_tenant=tenant,
        target_subject=subject,
        apply=args.apply,
    )
    print(f"Origen: {home}")
    print_report(report, applied=args.apply, tenant=tenant, email=args.email)

    if args.apply and args.sync and report.migrated_ids:
        queue = SqsWorkQueue(session.client("sqs"), outputs["work_queue_url"])
        service = ConnectionService(
            target.configs, target.secrets, queue, lambda: datetime.now(tz=UTC).date()
        )
        for connector_id in report.migrated_ids:
            task = service.sync(tenant, connector_id, days=30)
            print(f"  Sincronización en cola: {connector_id} ({task.start} → {task.end})")
        print("Los datos aparecerán en el dashboard en uno o dos minutos.")


if __name__ == "__main__":
    main()
