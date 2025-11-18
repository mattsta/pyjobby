.PHONY: help install test test-fast test-parallel coverage lint format type-check clean setup-db stop-db reset-db

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install dependencies
	@echo "Installing dependencies..."
	poetry install --with test,dev
	@echo "Done! Run 'make setup-db' to start test database."

setup-db:  ## Start PostgreSQL test database
	@./scripts/setup-test-db.sh

stop-db:  ## Stop PostgreSQL test database
	@./scripts/stop-test-db.sh

reset-db:  ## Reset PostgreSQL test database (wipe all data)
	@./scripts/reset-test-db.sh

test:  ## Run all tests
	@./scripts/run-tests.sh

test-fast:  ## Run fast tests only (skip slow/concurrency tests)
	@./scripts/run-tests.sh --fast

test-parallel:  ## Run tests in parallel
	@./scripts/run-tests.sh --parallel

coverage:  ## Run tests with coverage report
	@./scripts/run-tests.sh --cov --cov-report=html
	@echo ""
	@echo "Coverage report generated in htmlcov/index.html"

lint:  ## Run linter (ruff)
	@echo "Running ruff..."
	poetry run ruff check pyjobby/ tests/

format:  ## Format code with black
	@echo "Formatting code..."
	poetry run black pyjobby/ tests/

type-check:  ## Run type checker (mypy)
	@echo "Running mypy..."
	poetry run mypy pyjobby/

clean:  ## Clean up generated files
	@echo "Cleaning up..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name .coverage -delete 2>/dev/null || true
	@find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@echo "Done!"

ci:  ## Run all CI checks (format, lint, type-check, test)
	@echo "Running CI checks..."
	@make format
	@make lint
	@make type-check
	@make test
	@echo ""
	@echo "All CI checks passed! ✓"

dev:  ## Start development environment (database + pgAdmin)
	@echo "Starting development environment..."
	docker-compose --profile dev up -d
	@echo ""
	@echo "Development environment started!"
	@echo "  PostgreSQL: localhost:5433"
	@echo "  pgAdmin: http://localhost:5050"
	@echo "    Email: admin@pyjobby.test"
	@echo "    Password: admin"
