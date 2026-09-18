SHELL := /bin/bash
COMPOSE := docker compose --env-file .env.poc

.PHONY: validate up down logs bootstrap demo ps clean
validate:
	./scripts/validate.sh

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
