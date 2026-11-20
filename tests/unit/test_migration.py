"""Migración de la configuración local a otro despliegue (servicio ``migration``)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from radar_gastos.adapters.memory import (
    InMemoryConfigRepository,
    InMemoryMappingRepository,
    InMemoryRecurringRepository,
    StaticSecrets,
)
from radar_gastos.domain.models import Budget, ConnectorConfig, PrincipalMapping, Provider
from radar_gastos.domain.recurring import Profile, RecurringExpense
from radar_gastos.services.migration import Store, migrate

KEY = {"admin_api_key": "sk-admin-secreta"}


def store() -> Store:
    return Store(
        InMemoryConfigRepository(),
        InMemoryMappingRepository(),
        StaticSecrets(),
        InMemoryRecurringRepository(),
    )


@pytest.fixture
def local() -> Store:
    s = store()
    ref = s.secrets.put_secret("local", "openai-1", KEY)
    s.configs.put_connector(
        ConnectorConfig(
            tenant_id="local",
            connector_id="openai-1",
            provider=Provider.OPENAI,
            label="OpenAI",
            secret_arn=ref,
        )
    )
    s.configs.put_connector(
        ConnectorConfig(
            tenant_id="local",
            connector_id="cc-1",
            provider=Provider.CLAUDE_CODE,
            label="Claude Code",
            options={"path": "C:/x", "user": "jimmy"},
        )
    )
    # Datos de otro tenant (la vista de Ejemplo) no deben copiarse.
    s.configs.put_connector(
        ConnectorConfig(tenant_id="demo", connector_id="syn", provider=Provider.SYNTHETIC)
    )
    s.mappings.put_mapping(
        "local",
        PrincipalMapping(provider=Provider.OPENAI, principal="key-1", member="jimmy", team="datos"),
    )
    s.configs.put_budget(Budget(tenant_id="local", team="datos", monthly_limit=Decimal("100")))
    s.recurring.put_profile("user:local", Profile(profile_id="diana-1", name="Diana"))
    s.recurring.put_expense(
        "user:local",
        RecurringExpense(
            expense_id="netflix",
            name="Netflix",
            amount=Decimal("44900"),
            currency="COP",
            billing_date=date(2026, 11, 21),
            member_ids=("diana-1",),
        ),
    )
    s.recurring.put_expense(
        "tenant:local",
        RecurringExpense(
            expense_id="slack",
            name="Slack",
            category="colaboracion",
            amount=Decimal("8.75"),
            currency="USD",
            billing_date=date(2026, 12, 1),
            seats=5,
        ),
    )
    return s


def run(local: Store, target: Store, *, apply: bool):  # type: ignore[no-untyped-def]
    return migrate(
        local,
        target,
        source_tenant="local",
        source_subject="local",
        target_tenant="acme",
        target_subject="sub-123",
        apply=apply,
    )


def test_dry_run_reports_without_writing(local: Store) -> None:
    target = store()
    report = run(local, target, apply=False)

    assert report.connectors == ["OpenAI"]
    assert report.skipped and report.skipped[0][0] == "Claude Code"
    assert (report.mappings, report.budgets, report.profiles) == (1, 1, 1)
    assert (report.personal_expenses, report.business_expenses) == (1, 1)
    assert target.configs.list_connectors("acme") == []
    assert target.recurring.list_expenses("user:sub-123") == []


def test_apply_moves_everything_to_the_target_identity(local: Store) -> None:
    target = store()
    run(local, target, apply=True)

    (conn,) = target.configs.list_connectors("acme")
    assert conn.connector_id == "openai-1" and conn.tenant_id == "acme"
    assert (
        conn.secret_arn and conn.secret_arn != local.configs.list_connectors("local")[0].secret_arn
    )
    assert target.secrets.get_secret(conn.secret_arn) == KEY  # la llave viaja al nuevo almacén
    assert target.mappings.list_mappings("acme")[0].member == "jimmy"
    assert target.configs.list_budgets("acme")[0].tenant_id == "acme"
    # Lo personal queda a nombre del usuario de destino; lo de empresa, de su tenant.
    assert [p.name for p in target.recurring.list_profiles("user:sub-123")] == ["Diana"]
    (netflix,) = target.recurring.list_expenses("user:sub-123")
    assert netflix.member_ids == ("diana-1",)
    assert [e.name for e in target.recurring.list_expenses("tenant:acme")] == ["Slack"]


def test_repeating_the_migration_does_not_duplicate(local: Store) -> None:
    target = store()
    run(local, target, apply=True)
    run(local, target, apply=True)
    assert len(target.configs.list_connectors("acme")) == 1
    assert len(target.recurring.list_expenses("user:sub-123")) == 1
    assert len(target.recurring.list_profiles("user:sub-123")) == 1


def test_connection_whose_secret_is_missing_is_skipped(local: Store) -> None:
    local.configs.put_connector(
        ConnectorConfig(
            tenant_id="local",
            connector_id="anthropic-1",
            provider=Provider.ANTHROPIC,
            label="Anthropic",
            secret_arn="local:local/anthropic-1/perdido",
        )
    )
    report = run(local, store(), apply=True)
    assert ("Anthropic", "su credencial no está en el origen") in report.skipped


def test_report_never_contains_credentials(local: Store) -> None:
    report = run(local, store(), apply=True)
    assert "sk-admin-secreta" not in repr(report)
