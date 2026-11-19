"""Conectores de proveedores (patrones Adapter + Strategy + Registry).

Agregar un proveedor nuevo = crear un módulo que implemente ``UsageConnector`` y
registrarlo con ``@register``. Ningún otro componente cambia (principio Open/Closed).
"""

from radar_gastos.connectors.registry import (
    ConnectorFactory,
    build_connector,
    register,
    registered_providers,
)

__all__ = ["ConnectorFactory", "build_connector", "register", "registered_providers"]
