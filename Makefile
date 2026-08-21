.PHONY: help format format-check lint typecheck test start start-web start-api services-up stop services-down

help:
	@echo "CareerLens — available commands:"
	@echo "  make format         Auto-format web (Prettier) and API (Ruff)"
	@echo "  make format-check   Check formatting without writing changes"
	@echo "  make lint           Lint web (ESLint) and API (Ruff)"
	@echo "  make typecheck      Typecheck web (tsc) and API (mypy)"
	@echo "  make test           Run web (Vitest) and API (pytest) tests"
	@echo "  make start          Start Postgres + Redis via Docker Compose"
	@echo "  make start-web      Run the Next.js dev server (host, foreground)"
	@echo "  make start-api      Run the FastAPI dev server (host, foreground)"
	@echo "  make stop           Stop the Docker Compose data services"

format:
	cd apps/web && npm run format
	cd apps/api && uv run ruff format .

format-check:
	cd apps/web && npm run format:check
	cd apps/api && uv run ruff format --check .

lint:
	cd apps/web && npm run lint
	cd apps/api && uv run ruff check .

typecheck:
	cd apps/web && npm run typecheck
	cd apps/api && uv run mypy app

test:
	cd apps/web && npm run test
	cd apps/api && uv run pytest

# "start" brings up the data services only. The web and API apps run on the
# host directly (not in Docker) for easier debugging — start them with
# `make start-web` / `make start-api` in separate terminals.
start: services-up
	@echo "Postgres and Redis are up. In separate terminals, run:"
	@echo "  make start-web"
	@echo "  make start-api"

start-web:
	cd apps/web && npm run dev

start-api:
	cd apps/api && uv run uvicorn app.main:app --reload --port 8000

services-up:
	docker compose up -d

stop: services-down

services-down:
	docker compose down
