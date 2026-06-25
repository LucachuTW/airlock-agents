COMPOSE := docker compose -f deploy/docker-compose.yml

.PHONY: up down dev migrate seed test eval lint models

up:
	$(COMPOSE) up -d

observability:
	$(COMPOSE) --profile observability up -d

down:
	$(COMPOSE) down

dev:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	uv run alembic upgrade head

models:
	uv run python -m scripts.pull_models

data:
	uv run python -m scripts.fetch_data

seed: migrate models data
	uv run python -m scripts.seed

test:
	uv run pytest -q

eval:
	uv run python -m evals.run_offline --gate

lint:
	uv run ruff check . && uv run mypy app
