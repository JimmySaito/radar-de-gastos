PY ?= python
TF_DIR := infra/terraform/envs/dev

.PHONY: help install lint fmt typecheck test check run build tf-init tf-plan deploy load-test destroy

help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Instala dependencias de desarrollo
	$(PY) -m pip install -e ".[dev]"

fmt: ## Formatea el código
	ruff format src tests scripts
	ruff check --fix src tests scripts

lint: ## Linter
	ruff format --check src tests scripts
	ruff check src tests scripts

typecheck: ## Tipado estático estricto
	mypy

test: ## Pruebas unitarias + integración (moto) con cobertura
	pytest

check: lint typecheck test ## Todo lo que corre en CI

run: ## Demo local sin AWS en http://localhost:8080
	$(PY) scripts/local_server.py --port 8080

build: ## Empaqueta dist/app.zip y dist/layer.zip (Lambda arm64)
	$(PY) scripts/build_lambda.py

tf-init:
	cd $(TF_DIR) && terraform init

tf-plan: build
	cd $(TF_DIR) && terraform plan -out=tfplan

deploy: build ## Despliega en AWS (requiere terraform.tfvars)
	cd $(TF_DIR) && terraform apply

load-test: ## Prueba de estrés: make load-test SCENARIO=stress BASE_URL=... TOKEN=...
	k6 run -e BASE_URL=$(BASE_URL) -e TOKEN=$(TOKEN) -e SCENARIO=$(or $(SCENARIO),load) tests/load/k6_ingest.js

destroy: ## Elimina la infraestructura del entorno dev
	cd $(TF_DIR) && terraform destroy
