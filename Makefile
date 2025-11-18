.PHONY: help install install-postgres test test-fast test-parallel coverage lint format type-check clean setup-db stop-db reset-db

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python dependencies
	@echo "Installing dependencies..."
	poetry install --with test,dev
	@echo "Done! Run 'make install-postgres' if PostgreSQL is not installed."
	@echo "Then run 'make setup-db' to create test database."

install-postgres:  ## Install PostgreSQL natively
	@./scripts/install-postgres.sh

setup-db:  ## Create test database
	@./scripts/setup-test-db.sh

stop-db:  ## Stop PostgreSQL service (caution: affects all databases)
	@./scripts/stop-db.sh

reset-db:  ## Reset test database (wipe all data)
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
