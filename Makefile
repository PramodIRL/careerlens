.PHONY: help format format-check lint typecheck test start start-web start-api start-worker services-up stop services-down migrate migration migrate-status requeue-stuck-resumes seed-skills github-skills sample-resumes demo-github smoke

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
	@echo "  make start-worker   Run the Celery worker: resume extraction + GitHub ingestion"
	@echo "  make stop           Stop the Docker Compose data services"
	@echo "  make migrate        Apply all pending Alembic migrations"
	@echo "  make migrate-status Show the current Alembic migration"
	@echo "  make migration name=\"...\"  Create a new (blank) migration"
	@echo "  make requeue-stuck-resumes  Re-enqueue resumes stuck in 'queued' (safe to re-run)"
	@echo "  make seed-skills    Seed/update the canonical skill taxonomy (safe to re-run)"
	@echo "  make github-skills  Re-derive GitHub skill evidence from stored data (no GitHub calls)"
	@echo "  make sample-resumes Write the fictional demo resumes to apps/api/var/samples/"
	@echo "  make demo-github EMAIL=...  Import the fictional GitHub account for one user"
	@echo "  make smoke          Run the end-to-end developer smoke test"

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
	cd apps/api && uv run mypy app scripts

test:
	cd apps/web && npm run test
	cd apps/api && uv run pytest

# "start" brings up the data services only. The web and API apps run on the
# host directly (not in Docker) for easier debugging — start them with
# `make start-web` / `make start-api` / `make start-worker` in separate
# terminals.
start: services-up
	@echo "Postgres and Redis are up. In separate terminals, run:"
	@echo "  make start-web"
	@echo "  make start-api"
	@echo "  make start-worker  (processes resume extraction and GitHub imports)"

start-web:
	cd apps/web && npm run dev

start-api:
	cd apps/api && uv run uvicorn app.main:app --reload --port 8000

# Processes resume text-extraction jobs (Prompt 2.2) and GitHub
# ingestion runs (Prompt 3.2) — without this running, uploaded resumes
# and queued imports stay "queued" forever; nothing else in the app
# blocks on it. See apps/api/app/worker.py.
start-worker:
	cd apps/api && uv run celery -A app.worker worker --loglevel=info

services-up:
	docker compose up -d

stop: services-down

services-down:
	docker compose down

migrate:
	cd apps/api && uv run alembic upgrade head

migrate-status:
	cd apps/api && uv run alembic current

migration:
	cd apps/api && uv run alembic revision -m "$(name)"

# One-off reconciliation for the Prompt 2.1 -> 2.2 migration gap: any
# resume still in status "queued" (most likely a legacy row the
# migration renamed from "uploaded", or an upload whose enqueue silently
# failed) is re-enqueued through the normal extraction path. Safe to run
# more than once — see apps/api/scripts/requeue_stuck_resumes.py.
requeue-stuck-resumes:
	cd apps/api && uv run python -m scripts.requeue_stuck_resumes

# Seeds (or re-seeds) the canonical skill taxonomy from
# apps/api/app/seeds/skill_taxonomy.py. Safe and deterministic to run
# repeatedly — edit the seed file, re-run this. Skills coined by users
# are never touched. See apps/api/scripts/seed_skills.py.
seed-skills:
	cd apps/api && uv run python -m scripts.seed_skills

# Re-derives GitHub-backed skill evidence for every connected user from
# data ALREADY in Postgres — it makes no GitHub requests at all. Run this
# after `make seed-skills` changes the taxonomy: the stored repositories
# are still correct, only the derived evidence is stale, and re-importing
# to fix that would re-spend ~42 of GitHub's 60-requests-per-hour budget.
# Safe and deterministic to re-run; never changes a confirmed or rejected
# decision. See apps/api/scripts/extract_github_skills.py.
github-skills:
	cd apps/api && uv run python -m scripts.extract_github_skills

# Writes the fictional sample resumes (apps/api/scripts/sample_resumes.py)
# to apps/api/var/samples/, so a demo never needs a real person's resume.
# FILES ONLY: this writes nothing to the database and creates no users —
# the demo goes through the real upload/extraction flow. The output
# directory is inside the already-gitignored apps/api/var/, so a
# generated document cannot be committed. Safe to re-run; overwrites.
sample-resumes:
	cd apps/api && uv run python -m scripts.sample_resumes

# Imports the fictional GitHub account (apps/api/scripts/sample_github.py)
# for ONE already-registered user, running the real ingestion and evidence
# derivation with NO network request. This is what makes the GitHub half of
# the product demoable without pointing it at a real person's repositories
# or spending GitHub's 60-requests-per-hour budget. The user must already
# exist — this will not create one — and nothing outside that user's rows is
# touched. There is deliberately no fake-client mode inside the API server
# itself. Safe to re-run: a second run adds no duplicate repository, evidence
# or candidate skill and never changes a confirmed/rejected decision, though
# it does refresh each repository's last_seen_at. See
# docs/phase-4-acceptance.md.
demo-github:
ifndef EMAIL
	$(error EMAIL is required, e.g. make demo-github EMAIL=ada.sample@example.com)
endif
	cd apps/api && uv run python -m scripts.demo_github_import --email "$(EMAIL)"

smoke:
	./scripts/smoke.sh
