"""Persistencia local en archivos JSON para ejecutar UsageHub sin AWS de forma *usable*.

La configuración (conectores, mapeos, presupuestos, estado de sincronización) y las
credenciales sobreviven a reinicios del servidor local. Los hechos de consumo se
mantienen en memoria y se reconstruyen sincronizando con los proveedores al arrancar
(el ledger idempotente hace que releer sea seguro).

Ubicación: ``$USAGEHUB_HOME`` o ``~/.usagehub`` — fuera del repositorio, para que las
llaves nunca terminen en git. En Windows el perfil del usuario ya restringe el acceso a
su dueño; en POSIX se fuerza el modo 0600.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from usage_hub.adapters.memory import (
    InMemoryConfigRepository,
    InMemoryMappingRepository,
    InMemoryRecurringRepository,
    StaticSecrets,
)
from usage_hub.domain.models import (
    Budget,
    ConnectorConfig,
    ConnectorStatus,
    PrincipalMapping,
    Provider,
)
from usage_hub.domain.recurring import Profile, RecurringExpense


def default_home() -> Path:
    return Path(os.environ.get("USAGEHUB_HOME") or Path.home() / ".usagehub")


class _JsonFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        result: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return result

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )
            if os.name == "posix":
                tmp.chmod(0o600)
            tmp.replace(self.path)  # escritura atómica


class FileConfigRepository(InMemoryConfigRepository):
    def __init__(self, home: Path) -> None:
        super().__init__()
        self._file = _JsonFile(home / "config.json")
        data = self._file.load()
        for raw in data.get("connectors", []):
            config = ConnectorConfig.model_validate(raw)
            self.connectors[(config.tenant_id, config.connector_id)] = config
        for raw in data.get("statuses", []):
            self.statuses[(raw["tenant_id"], raw["connector_id"])] = ConnectorStatus.model_validate(
                raw["status"]
            )
        for raw in data.get("budgets", []):
            budget = Budget.model_validate(raw)
            self.budgets[(budget.tenant_id, budget.team, budget.provider or "*")] = budget
        self.sent_alerts = set(data.get("sent_alerts", []))

    def _changed(self) -> None:
        self._file.save(
            {
                "saved_at": datetime.now().isoformat(),
                "connectors": [c.model_dump(mode="json") for c in self.connectors.values()],
                "statuses": [
                    {"tenant_id": t, "connector_id": c, "status": s.model_dump(mode="json")}
                    for (t, c), s in self.statuses.items()
                ],
                "budgets": [b.model_dump(mode="json") for b in self.budgets.values()],
                "sent_alerts": sorted(self.sent_alerts),
            }
        )


class FileMappingRepository(InMemoryMappingRepository):
    def __init__(self, home: Path) -> None:
        super().__init__()
        self._file = _JsonFile(home / "mappings.json")
        for raw in self._file.load().get("mappings", []):
            mapping = PrincipalMapping.model_validate(raw["mapping"])
            self._items[(raw["tenant_id"], Provider(mapping.provider), mapping.principal)] = mapping

    def _changed(self) -> None:
        self._file.save(
            {
                "mappings": [
                    {"tenant_id": t, "mapping": m.model_dump(mode="json")}
                    for (t, _, _), m in self._items.items()
                ]
            }
        )


class FileSecretStore(StaticSecrets):
    def __init__(self, home: Path) -> None:
        self._file = _JsonFile(home / "secrets.json")
        super().__init__(self._file.load().get("secrets", {}))

    def _changed(self) -> None:
        self._file.save({"secrets": self._secrets})


class FileRecurringRepository(InMemoryRecurringRepository):
    def __init__(self, home: Path) -> None:
        super().__init__()
        self._file = _JsonFile(home / "recurring.json")
        for owner, items in self._file.load().get("owners", {}).items():
            for raw in items:
                expense = RecurringExpense.model_validate(raw)
                self._items[owner][expense.expense_id] = expense
        for owner, items in self._file.load().get("profiles", {}).items():
            for raw in items:
                profile = Profile.model_validate(raw)
                self._profiles[owner][profile.profile_id] = profile

    def _changed(self) -> None:
        self._file.save(
            {
                "owners": {
                    owner: [e.model_dump(mode="json") for e in items.values()]
                    for owner, items in self._items.items()
                },
                "profiles": {
                    owner: [p.model_dump(mode="json") for p in items.values()]
                    for owner, items in self._profiles.items()
                },
            }
        )
