"""Empaqueta el código (dist/app.zip) y las dependencias (dist/layer.zip) para Lambda arm64.

Funciona en Windows, macOS y Linux: pip descarga wheels manylinux para aarch64, así
que no hace falta Docker. boto3 se excluye porque ya viene en el runtime de Lambda.

    python scripts/build_lambda.py
"""

from __future__ import annotations

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


def zip_dir(source: Path, target: Path, arc_root: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                zf.write(path, path.relative_to(arc_root))


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
            "--target",
            str(BUILD),
            "--platform",
            "manylinux2014_aarch64",
            "--implementation",
            "cp",
            "--python-version",
            "3.13",
            "--only-binary=:all:",
            "--upgrade",
            *RUNTIME_DEPS,
        ],
        check=True,
    )
    for name in ("boto3", "botocore", "s3transfer"):  # incluidos en el runtime
        for path in BUILD.glob(f"{name}*"):
            shutil.rmtree(path, ignore_errors=True)
    zip_dir(
        BUILD.parent, DIST / "layer.zip", BUILD.parent
    )  # entradas "python/..." (formato de layer)
    zip_dir(ROOT / "src" / "radar_gastos", DIST / "app.zip", ROOT / "src")
    # El dashboard viaja en el paquete por si la API debe servirlo (sin CloudFront).
    with zipfile.ZipFile(DIST / "app.zip", "a", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted((ROOT / "frontend").rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(ROOT))
    for artifact in ("layer.zip", "app.zip"):
        print(f"{artifact}: {(DIST / artifact).stat().st_size / 1_048_576:.1f} MiB")


if __name__ == "__main__":
    main()
