"""Catálogo de servicios recurrentes comunes, para autocompletar el formulario.

No incluye precios: varían por país, plan y en el tiempo, así que siempre los ingresa
el usuario. Solo sugiere nombre, categoría y ciclo habitual.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from usage_hub.domain.recurring import BillingCycle, Scope

# Categorías en orden fijo (el orden define el color en el panel; el resto cae en "otros").
PERSONAL_CATEGORIES: dict[str, str] = {
    "streaming": "Streaming de video",
    "musica": "Música y audio",
    "ia": "Inteligencia artificial",
    "software": "Software y productividad",
    "nube": "Almacenamiento en la nube",
    "gaming": "Videojuegos",
    "bienestar": "Salud y bienestar",
    "otros": "Otros",
}
BUSINESS_CATEGORIES: dict[str, str] = {
    "ia": "Inteligencia artificial",
    "colaboracion": "Colaboración y comunicación",
    "desarrollo": "Desarrollo de software",
    "productividad": "Productividad y oficina",
    "diseno": "Diseño",
    "nube": "Nube e infraestructura",
    "seguridad": "Seguridad",
    "otros": "Otros",
}


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    key: str
    name: str
    category: str
    cycle: BillingCycle = BillingCycle.MONTHLY
    plans: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cycle"] = self.cycle.value
        return data


PERSONAL_SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        "netflix", "Netflix", "streaming", plans=("Básico con anuncios", "Estándar", "Premium")
    ),
    ServiceSpec(
        "max", "HBO Max", "streaming", plans=("Básico con anuncios", "Estándar", "Platino")
    ),
    ServiceSpec("disney", "Disney+", "streaming", plans=("Estándar", "Premium")),
    ServiceSpec("prime", "Prime Video / Amazon Prime", "streaming"),
    ServiceSpec("appletv", "Apple TV+", "streaming"),
    ServiceSpec("paramount", "Paramount+", "streaming"),
    ServiceSpec("crunchyroll", "Crunchyroll", "streaming"),
    ServiceSpec("youtube", "YouTube Premium", "streaming", plans=("Individual", "Familiar")),
    ServiceSpec(
        "spotify", "Spotify", "musica", plans=("Individual", "Duo", "Familiar", "Estudiantes")
    ),
    ServiceSpec("applemusic", "Apple Music", "musica"),
    ServiceSpec("deezer", "Deezer", "musica"),
    ServiceSpec("chatgpt", "ChatGPT", "ia", plans=("Plus", "Pro")),
    ServiceSpec("claude", "Claude", "ia", plans=("Pro", "Max")),
    ServiceSpec("gemini", "Google AI (Gemini)", "ia", plans=("AI Pro", "AI Ultra")),
    ServiceSpec("copilot", "Microsoft Copilot Pro", "ia"),
    ServiceSpec("m365", "Microsoft 365", "software", BillingCycle.ANNUAL, ("Personal", "Familia")),
    ServiceSpec("adobe", "Adobe Creative Cloud", "software"),
    ServiceSpec("canva", "Canva Pro", "software", BillingCycle.ANNUAL),
    ServiceSpec("icloud", "iCloud+", "nube"),
    ServiceSpec("googleone", "Google One", "nube"),
    ServiceSpec("dropbox", "Dropbox", "nube", BillingCycle.ANNUAL),
    ServiceSpec("gamepass", "Xbox Game Pass", "gaming"),
    ServiceSpec("psplus", "PlayStation Plus", "gaming", BillingCycle.ANNUAL),
    ServiceSpec("nintendo", "Nintendo Switch Online", "gaming", BillingCycle.ANNUAL),
    ServiceSpec("gym", "Gimnasio", "bienestar"),
    ServiceSpec("duolingo", "Duolingo Super", "bienestar", BillingCycle.ANNUAL),
    ServiceSpec("other", "Otro", "otros"),
)

BUSINESS_SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec("chatgpt-business", "ChatGPT Business / Enterprise", "ia"),
    ServiceSpec("claude-team", "Claude Team / Enterprise", "ia"),
    ServiceSpec("gemini-workspace", "Gemini para Google Workspace", "ia"),
    ServiceSpec("copilot-m365", "Microsoft 365 Copilot", "ia"),
    ServiceSpec("github-copilot", "GitHub Copilot Business", "desarrollo"),
    ServiceSpec("slack", "Slack", "colaboracion"),
    ServiceSpec("teams", "Microsoft Teams", "colaboracion"),
    ServiceSpec("zoom", "Zoom", "colaboracion"),
    ServiceSpec("google-workspace", "Google Workspace", "productividad"),
    ServiceSpec("m365-business", "Microsoft 365 Business", "productividad"),
    ServiceSpec("notion", "Notion", "productividad"),
    ServiceSpec("github", "GitHub", "desarrollo"),
    ServiceSpec("atlassian", "Jira / Confluence", "desarrollo"),
    ServiceSpec("figma", "Figma", "diseno"),
    ServiceSpec("adobe-business", "Adobe Creative Cloud para equipos", "diseno"),
    ServiceSpec("1password", "Gestor de contraseñas", "seguridad"),
    ServiceSpec("other", "Otro", "otros"),
)


def catalog(scope: Scope) -> dict[str, Any]:
    personal = scope is Scope.PERSONAL
    services = PERSONAL_SERVICES if personal else BUSINESS_SERVICES
    categories = PERSONAL_CATEGORIES if personal else BUSINESS_CATEGORIES
    return {
        "scope": scope.value,
        "categories": [{"key": k, "label": v} for k, v in categories.items()],
        "services": [s.as_dict() for s in services],
        "cycles": [
            {"key": BillingCycle.WEEKLY.value, "label": "Semanal"},
            {"key": BillingCycle.MONTHLY.value, "label": "Mensual"},
            {"key": BillingCycle.QUARTERLY.value, "label": "Trimestral"},
            {"key": BillingCycle.ANNUAL.value, "label": "Anual"},
        ],
    }


def valid_categories(scope: Scope) -> set[str]:
    return set(PERSONAL_CATEGORIES if scope is Scope.PERSONAL else BUSINESS_CATEGORIES)
