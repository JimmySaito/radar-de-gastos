"""Catálogo declarativo de proveedores: qué credenciales y opciones pide cada conector.

La UI de configuración se genera a partir de este catálogo (``GET /v1/providers``), así
que agregar un proveedor no requiere tocar el frontend. Este módulo no importa SDKs:
se puede cargar en el Lambda de la API sin costo de arranque.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from radar_gastos.domain.models import Provider

FieldKind = Literal["text", "password", "textarea", "number", "select", "date"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    label: str
    kind: FieldKind = "text"
    required: bool = True
    secret: bool = False  # True → se guarda en el almacén de secretos, nunca se devuelve
    placeholder: str = ""
    help: str = ""
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (valor, etiqueta) para kind="select"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider: Provider
    name: str
    description: str
    docs_url: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    notes: str = ""

    @property
    def secret_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.secret)

    @property
    def option_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if not f.secret)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["provider"] = self.provider.value
        return data


CATALOG: dict[Provider, ProviderSpec] = {
    spec.provider: spec
    for spec in (
        ProviderSpec(
            provider=Provider.ANTHROPIC,
            name="Anthropic (Claude)",
            description="Tokens y costo por persona, service account o API key y por modelo.",
            docs_url="https://platform.claude.com/docs/en/manage-claude/usage-cost-analytics-api",
            fields=(
                FieldSpec(
                    "admin_api_key",
                    "API key con alcance de organización",
                    kind="password",
                    secret=True,
                    placeholder="sk-ant-...",
                    help="Claude Console → Claves → Crear clave, con alcance «Organización» "
                    "(no un espacio de trabajo) y ligada a un usuario admin o a una cuenta de "
                    "servicio. También sirve una Admin key (sk-ant-admin...). En Claude "
                    "Enterprise: key con scope read:analytics.",
                ),
            ),
            notes="Los datos llegan con 4-24 h de retraso.",
        ),
        ProviderSpec(
            provider=Provider.OPENAI,
            name="OpenAI",
            description="Tokens por usuario o API key y costo por proyecto.",
            docs_url="https://platform.openai.com/docs/api-reference/usage",
            fields=(
                FieldSpec(
                    "admin_api_key",
                    "Admin key",
                    kind="password",
                    secret=True,
                    placeholder="sk-admin-...",
                    help="platform.openai.com → Organization → Admin keys (solo owners).",
                ),
            ),
            notes="OpenAI no expone el costo por usuario: se atribuye al proyecto.",
        ),
        ProviderSpec(
            provider=Provider.AWS,
            name="AWS (Cost Explorer)",
            description="Costo diario por servicio (incluye Amazon Bedrock) y por tag de dueño.",
            docs_url="https://docs.aws.amazon.com/cost-management/latest/userguide/ce-api.html",
            fields=(
                FieldSpec(
                    "role_arn",
                    "ARN del rol a asumir",
                    required=False,
                    secret=True,
                    placeholder="arn:aws:iam::123456789012:role/radar-gastos-cost-reader",
                    help="Opcional. Vacío = usa las credenciales del entorno "
                    "(perfil de AWS CLI en local, rol del Lambda en la nube).",
                ),
                FieldSpec(
                    "external_id",
                    "External ID",
                    kind="password",
                    required=False,
                    secret=True,
                    help="Solo si el rol lo exige en su política de confianza.",
                ),
                FieldSpec(
                    "owner_tag",
                    "Tag de dueño",
                    required=False,
                    default="owner",
                    help="Cost allocation tag que identifica a la persona o equipo responsable.",
                ),
            ),
            notes="Cada consulta a Cost Explorer cuesta USD 0,01.",
        ),
        ProviderSpec(
            provider=Provider.GOOGLE,
            name="Google (Gemini / Vertex AI)",
            description="Costo diario desde la exportación de Cloud Billing a BigQuery.",
            docs_url="https://cloud.google.com/billing/docs/how-to/export-data-bigquery",
            fields=(
                FieldSpec(
                    "service_account_json",
                    "Service account (JSON)",
                    kind="textarea",
                    secret=True,
                    help="Cuenta con roles BigQuery Data Viewer y BigQuery Job User.",
                ),
                FieldSpec(
                    "billing_table",
                    "Tabla de exportación",
                    placeholder="proyecto.dataset.gcp_billing_export_v1_XXXXXX",
                ),
                FieldSpec("owner_label", "Etiqueta de dueño", required=False, default="owner"),
            ),
            notes="Requiere `pip install radar-de-gastos[gcp]`.",
        ),
        ProviderSpec(
            provider=Provider.CLAUDE_CODE,
            name="Claude Code (este equipo)",
            description="Tokens exactos de Claude Code leídos de sus registros locales, "
            "también con suscripción Pro/Max.",
            docs_url="https://code.claude.com/docs/en/monitoring-usage",
            fields=(
                FieldSpec(
                    "path",
                    "Carpeta de Claude Code",
                    required=False,
                    default="~/.claude",
                    help="Donde Claude Code guarda sus sesiones (subcarpeta projects).",
                ),
                FieldSpec(
                    "user",
                    "Persona",
                    required=False,
                    placeholder="usuario del sistema",
                    help="De quién es este consumo. Vacío = usuario del sistema operativo.",
                ),
            ),
            notes="El costo es el equivalente en API (estimado); con suscripción pagas una "
            "tarifa fija. Para un equipo, cada persona usa scripts/push_claude_code.py.",
        ),
        ProviderSpec(
            provider=Provider.SYNTHETIC,
            name="Generador de prueba",
            description="Datos sintéticos para probar el flujo completo sin credenciales.",
            docs_url="",
            fields=(
                FieldSpec(
                    "members", "Personas simuladas", kind="number", required=False, default="5"
                ),
                FieldSpec("seed", "Semilla", kind="number", required=False, default="1"),
            ),
        ),
    )
}


class InvalidConfigurationError(ValueError):
    pass


def spec_for(provider: Provider) -> ProviderSpec:
    try:
        return CATALOG[provider]
    except KeyError as exc:
        raise InvalidConfigurationError(
            f"el proveedor {provider} no se configura con credenciales"
        ) from exc


def split_values(
    provider: Provider, values: Mapping[str, str], existing_secret: Mapping[str, str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Separa los valores del formulario en (secretos, opciones) y valida obligatorios.

    Un campo secreto vacío conserva el valor ya guardado: así la UI puede editar opciones
    sin que el usuario vuelva a pegar la llave (que nunca se le devuelve).
    """
    spec = spec_for(provider)
    known = {f.name for f in spec.fields}
    if unknown := set(values) - known:
        raise InvalidConfigurationError(f"campos desconocidos: {', '.join(sorted(unknown))}")
    secrets: dict[str, str] = {}
    options: dict[str, str] = {}
    previous = dict(existing_secret or {})
    for f in spec.fields:
        raw = (values.get(f.name) or "").strip()
        if f.secret:
            value = raw or previous.get(f.name, "")
            if value:
                secrets[f.name] = value
        else:
            value = raw or f.default
            if value:
                options[f.name] = value
        if f.required and not value:
            raise InvalidConfigurationError(f"falta el campo obligatorio: {f.label}")
        if value and f.choices and value not in {c for c, _ in f.choices}:
            raise InvalidConfigurationError(f"valor no válido para {f.label}: {value}")
        if value and f.kind == "number":
            try:
                float(value.replace(",", "."))
            except ValueError as exc:
                raise InvalidConfigurationError(f"{f.label} debe ser un número") from exc
    return secrets, options
