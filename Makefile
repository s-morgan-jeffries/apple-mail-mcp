.PHONY: install dev test test-unit test-integration test-e2e lint format typecheck complexity audit check-all coverage clean

install:
	uv sync

dev:
	uv sync --dev

test:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" -q

test-unit:
	uv run pytest tests/unit/ -q

test-integration:
	MAIL_TEST_MODE=true uv run pytest tests/integration/ --run-integration -v

test-e2e:
	MAIL_TEST_MODE=true uv run pytest tests/e2e/ -v

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

typecheck:
	uv run mypy src/

complexity:
	@./scripts/check_complexity.sh

audit:
	@./scripts/check_dependencies.sh

coverage:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" --cov=apple_mail_mcp --cov-report=term-missing -q

check-all: lint typecheck test complexity
	@./scripts/check_version_sync.sh
	@./scripts/check_client_server_parity.sh
	@echo ""
	@echo "All checks passed."

clean:
	rm -rf __pycache__ .pytest_cache .coverage htmlcov/ .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
