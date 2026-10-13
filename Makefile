SHELL := /bin/bash
COMPOSE := docker compose --env-file .env.poc
ARGUS_BUILD_ID ?= $(shell git rev-parse HEAD)
S4_PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: validate validate-i18n build-langfuse-i18n validate-langfuse-integration up down logs bootstrap demo ps clean trajectory-prepare trajectory-test
validate:
	./scripts/validate.sh

# 准备阶段允许下载固定镜像/哈希锁定依赖；测试阶段只使用预装的本地制品。
trajectory-prepare:
	$(S4_PYTHON) -c 'import sys; assert sys.version_info[:2] == (3, 12), "S4 requires Python 3.12"'
	$(S4_PYTHON) -m venv tests/fixtures/trajectory_agents/.venv
	tests/fixtures/trajectory_agents/.venv/bin/python -m pip install --require-hashes -r tests/fixtures/trajectory_agents/requirements.txt
	$(S4_PYTHON) tests/fixtures/trajectory_agents/prepare.py

trajectory-test:
	$(S4_PYTHON) -m pytest -q tests/test_trajectory_backend_integration.py tests/test_trajectory_observation_integration.py -m trajectory_local

validate-i18n:
	./deploy/langfuse/scripts/validate-i18n.sh

build-langfuse-i18n:
	./deploy/langfuse/scripts/build-image.sh

validate-langfuse-integration:
	./deploy/langfuse/scripts/validate-integration.sh

up:
	ARGUS_BUILD_ID=$(ARGUS_BUILD_ID) $(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

bootstrap:
	./scripts/bootstrap.sh

demo:
	./scripts/run-demo.sh

clean:
	$(COMPOSE) down -v --remove-orphans

console-dev:
	pnpm --dir services/console dev

console-build:
	pnpm --dir services/console build

console-test:
	pnpm --dir services/console test

# Playwright drives `vite preview`, which serves dist/. Without the build
# first, the suite silently tests the previous bundle.
console-e2e: console-build
	pnpm --dir services/console test:e2e
