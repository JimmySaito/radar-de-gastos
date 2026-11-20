"""Copia la configuración de un despliegue a otro (p. ej. de local a AWS) sin rehacerla a mano.

Trabaja solo con los puertos del dominio, así que sirve entre cualquier par de almacenes:
archivos locales, memoria o DynamoDB + Secrets Manager. Copia las conexiones (con sus
credenciales, que pasan al almacén de secretos de destino), los mapeos, los presupuestos, las
licencias de la empresa y las suscripciones y personas del espacio personal.

No copia el historial de consumo: el destino lo vuelve a leer de cada proveedor al sincronizar,
y así queda consistente con su propio ledger. Tampoco copia conexiones que solo funcionan en el
equipo de origen (Claude Code lee archivos locales).

Es idempotente: conserva los identificadores, así que repetirla actualiza en vez de duplicar.
Nunca devuelve ni registra valores de credenciales.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from radar_gastos.domain.models import ConnectorConfig, Provider
from radar_gastos.domain.ports import (
    ConfigRepository,
    MappingRepository,
    RecurringRepository,
    SecretStore,
)
from radar_gastos.domain.recurring import Scope
from radar_gastos.services.recurring import owner_key

# Proveedores cuyo conector depende del equipo donde corre y no puede ejecutarse en la nube.
LOCAL_ONLY = {
    Provider.CLAUDE_CODE: (
        "lee los registros de Claude Code de este equipo; envía su uso a AWS con "
        "scripts/push_claude_code.py"
    ),
}


@dataclass(frozen=True)
class Store:
    """Los almacenes de un despliegue."""

    configs: ConfigRepository
    mappings: MappingRepository
    secrets: SecretStore
    recurring: RecurringRepository


@dataclass
class MigrationReport:
    connectors: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    mappings: int = 0
    budgets: int = 0
    business_expenses: int = 0
    personal_expenses: int = 0
    profiles: int = 0
    migrated_ids: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.connectors)
            + self.mappings
            + self.budgets
            + self.business_expenses
            + self.personal_expenses
            + self.profiles
        )


def migrate(
    source: Store,
    target: Store,
    *,
    source_tenant: str,
    source_subject: str,
    target_tenant: str,
    target_subject: str,
    apply: bool,
) -> MigrationReport:
    """Copia de ``source`` a ``target``. Con ``apply=False`` solo informa lo que haría."""
    run = _Run(source, target, source_tenant, source_subject, target_tenant, target_subject, apply)
    _connectors(run)
    _governance(run)
    _recurring(run)
    return run.report


@dataclass
class _Run:
    source: Store
    target: Store
    src_tenant: str
    src_subject: str
    dst_tenant: str
    dst_subject: str
    apply: bool
    report: MigrationReport = field(default_factory=MigrationReport)

    def owners(self, scope: Scope) -> tuple[str, str]:
        return (
            owner_key(scope, tenant_id=self.src_tenant, subject=self.src_subject),
            owner_key(scope, tenant_id=self.dst_tenant, subject=self.dst_subject),
        )


def _connectors(run: _Run) -> None:
    for config in run.source.configs.list_connectors(run.src_tenant):
        label = config.label or config.connector_id
        reason = LOCAL_ONLY.get(config.provider)
        if reason:
            run.report.skipped.append((label, reason))
            continue
        ref = config.secret_arn
        secrets = dict(run.source.secrets.get_secret(ref)) if ref else {}
        if ref and not secrets:
            run.report.skipped.append((label, "su credencial no está en el origen"))
            continue
        if run.apply:
            new_ref = (
                run.target.secrets.put_secret(run.dst_tenant, config.connector_id, secrets)
                if secrets
                else None
            )
            run.target.configs.put_connector(_moved(config, run.dst_tenant, new_ref))
        run.report.connectors.append(label)
        run.report.migrated_ids.append(config.connector_id)


def _governance(run: _Run) -> None:
    for mapping in run.source.mappings.list_mappings(run.src_tenant):
        if run.apply:
            run.target.mappings.put_mapping(run.dst_tenant, mapping)
        run.report.mappings += 1
    for budget in run.source.configs.list_budgets(run.src_tenant):
        if run.apply:
            run.target.configs.put_budget(budget.model_copy(update={"tenant_id": run.dst_tenant}))
        run.report.budgets += 1


def _recurring(run: _Run) -> None:
    personal, business = run.owners(Scope.PERSONAL), run.owners(Scope.BUSINESS)
    # Primero las personas: las suscripciones compartidas apuntan a sus identificadores.
    for profile in run.source.recurring.list_profiles(personal[0]):
        if run.apply:
            run.target.recurring.put_profile(personal[1], profile)
        run.report.profiles += 1
    for expense in run.source.recurring.list_expenses(personal[0]):
        if run.apply:
            run.target.recurring.put_expense(personal[1], expense)
        run.report.personal_expenses += 1
    for expense in run.source.recurring.list_expenses(business[0]):
        if run.apply:
            run.target.recurring.put_expense(business[1], expense)
        run.report.business_expenses += 1


def _moved(config: ConnectorConfig, tenant_id: str, secret_ref: str | None) -> ConnectorConfig:
    return config.model_copy(update={"tenant_id": tenant_id, "secret_arn": secret_ref})
