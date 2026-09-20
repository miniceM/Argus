SHELL := /bin/bash
COMPOSE := docker compose --env-file .env.poc

.PHONY: validate validate-i18n build-langfuse-i18n validate-langfuse-integration up down logs bootstrap demo ps clean
validate:
	./scripts/validate.sh

validate-i18n:
	./deploy/langfuse/scripts/validate-i18n.sh

build-langfuse-i18n:
	./deploy/langfuse/scripts/build-image.sh

validate-langfuse-integration:
	./deploy/langfuse/scripts/validate-integration.sh

up:
	$(COMPOSE) up -d --build

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
