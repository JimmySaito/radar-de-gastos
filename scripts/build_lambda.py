"""Empaqueta el código (dist/app.zip) y las dependencias (dist/layer.zip) para Lambda arm64.

Funciona en Windows, macOS y Linux: pip descarga wheels manylinux para aarch64, así
que no hace falta Docker. boto3 se excluye porque ya viene en el runtime de Lambda.

    python scripts/build_lambda.py
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "layer" / "python"
RUNTIME_DEPS = [
    "pydantic>=2.9,<3",
    "httpx>=0.27,<1",
    "anthropic>=2.9,<3",
    "aws-lambda-powertools[tracer]>=3.0,<4",
]


# Fecha fija para todas las entradas: el zip depende solo del contenido. Sin esto, pip extrae
# las dependencias con la fecha del momento, el hash cambia en cada build y Terraform reemplaza
# la capa en cada despliegue aunque las dependencias sean las mismas.
FIXED_DATE = (1980, 1, 1, 0, 0, 0)  # la fecha mínima que admite el formato zip


def _add(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # Unix, para que los permisos de abajo valgan en cualquier sistema
    info.external_attr = 0o644 << 16
    zf.writestr(info, path.read_bytes(), compresslevel=9)


def _files(source: Path) -> list[Path]:
    return sorted(
        (p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
        key=lambda p: p.relative_to(source).as_posix(),  # mismo orden en Windows y Linux
    )


def zip_dir(source: Path, target: Path, arc_root: Path) -> None:
    """Zip reproducible: mismo contenido, mismos bytes (orden, fechas y permisos fijos)."""
    with zipfile.ZipFile(target, "w") as zf:
        for path in _files(source):
            if path.suffix != ".pyc":
                _add(zf, path, path.relative_to(arc_root).as_posix())


def main() -> None:
    DIST.mkdir(exist_ok=True)
    shutil.rmtree(BUILD.parent, ignore_errors=True)
    BUILD.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",  # sin el aviso de "nueva versión de pip"
            "--target",
            str(BUILD),
            "--platform",
            "manylinux2014_aarch64",
            "--implementation",
            "cp",
            "--python-version",
            "3.13",
            "--only-binary=:all:",
            "--no-compile",  # sin .pyc: llevan fechas y no se empaquetan
            "--upgrade",
            *RUNTIME_DEPS,
        ],
        check=True,
    )
    for name in ("boto3", "botocore", "s3transfer"):  # incluidos en el runtime
        for path in BUILD.glob(f"{name}*"):
            shutil.rmtree(path, ignore_errors=True)
    # bin/: lanzadores de consola que pip genera para el sistema que empaqueta (.exe con fecha
    # incrustada en Windows, scripts con la ruta local en Linux); en Lambda no sirven.
    # RECORD: lista sus hashes y solo se usa para desinstalar.
    shutil.rmtree(BUILD / "bin", ignore_errors=True)
    for record in BUILD.glob("*.dist-info/RECORD"):
        record.unlink()
    zip_dir(
        BUILD.parent, DIST / "layer.zip", BUILD.parent
    )  # entradas "python/..." (formato de layer)
    zip_dir(ROOT / "src" / "radar_gastos", DIST / "app.zip", ROOT / "src")
    # El dashboard viaja en el paquete por si la API debe servirlo (sin CloudFront).
    with zipfile.ZipFile(DIST / "app.zip", "a") as zf:
        for path in _files(ROOT / "frontend"):
            _add(zf, path, path.relative_to(ROOT).as_posix())
    for artifact in ("layer.zip", "app.zip"):
        data = (DIST / artifact).read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:12]
        print(f"{artifact}: {len(data) / 1_048_576:.1f} MiB  sha256 {digest}")


if __name__ == "__main__":
    main()
