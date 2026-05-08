.PHONY: help install format check typecheck test test-live test-smoke verify clean run lint

help:  ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install project with dev dependencies
	uv pip install -e ".[dev]"
	uv run pre-commit install

format:  ## Auto-format code with ruff
	uv run ruff format src/ tests/

check:  ## Run linting checks
	uv run ruff check src/ tests/

lint:  ## Run custom architectural lint rules (CE001–CE005)
	uv run pytest tests/test_custom_lint.py -v --tb=short --no-header -p no:warnings

typecheck:  ## Run type checking with pyright
	uv run pyright

test:  ## Run test suite (excludes live + lint tests; run `make lint` for those)
	uv run pytest -n auto -m "not live and not lint" tests/

test-live:  ## Run live-only tests (real Anthropic API + claude CLI; requires ANTHROPIC_API_KEY)
	uv run pytest -m live tests/ -v

test-cov:  ## Run tests with coverage report
	uv run pytest tests/ -n auto -m "not live" --cov=coder_eval --cov-report=term-missing --cov-report=html
	@echo "📊 Coverage report: htmlcov/index.html"


verify:  ## Run all verification steps (CI equivalent)
	uv run ruff format --check src/ tests/
	uv run ruff check src/ tests/
	uv run pyright
	uv run pytest tests/test_custom_lint.py -v --tb=short --no-header -p no:warnings
	# uv run pip-audit --desc --skip-editable
	# uv run bandit -r src/ -ll --format json -o bandit-report.json
	uv run pytest tests/ -n auto -m "not live and not lint" --cov=coder_eval --cov-report=term-missing --cov-report=xml --cov-fail-under=80

clean:  ## Clean build artifacts and cache
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache .coverage coverage.xml htmlcov/ bandit-report.json coverage.xml
	rm -rf runs/2025-* runs/latest
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

run:	## Run coder-eval on all tasks with 8 parallel jobs
	uv run coder-eval run tasks/*.yaml -j 8

test-smoke:  ## Run e2e smoke tests with real API (mirrors CI "E2E Smoke Tests" job)
	uv run coder-eval run tasks/*.yaml --tags smoke-pass --model claude-haiku-4-5-20251001
	@echo "--- now running smoke-fail bucket (expected to exit non-zero) ---"
	! uv run coder-eval run tasks/*.yaml --tags smoke-fail --model claude-haiku-4-5-20251001

.DEFAULT_GOAL := help
