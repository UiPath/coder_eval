.PHONY: help install format check typecheck test verify clean e2e run

help:  ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install project with dev dependencies
	uv pip install -e ".[dev]"
	uv run pre-commit install

format:  ## Auto-format code with ruff
	uv run ruff format coder_eval/ tests/

check:  ## Run linting checks
	uv run ruff check coder_eval/ tests/

typecheck:  ## Run type checking with pyright
	uv run pyright

test:  ## Run test suite
	uv run pytest -v tests/

test-cov:  ## Run tests with coverage report
	pytest tests/ -v --cov=coder_eval --cov-report=term-missing --cov-report=html
	@echo "📊 Coverage report: htmlcov/index.html"


verify:  ## Run all verification steps (CI equivalent)
	uv run ruff format --check coder_eval/ tests/
	uv run ruff check coder_eval/ tests/
	uv run pyright
	# uv run pip-audit --desc --skip-editable
	# uv run bandit -r coder_eval/ -ll --format json -o bandit-report.json
	uv run pytest tests/ -v --cov=coder_eval --cov-report=term-missing --cov-report=xml --cov-fail-under=80

clean:  ## Clean build artifacts and cache
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache .coverage coverage.xml htmlcov/ bandit-report.json coverage.xml
	rm -rf runs/2025-* runs/latest
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

run:	## Run coder-eval on all tasks with 8 parallel jobs
	uv run coder-eval run tasks/*.yaml -j 8

e2e:  ## Run e2e smoke tests with real API
	uv run coder-eval run tasks/*.yaml --tags smoke --model claude-haiku-4-5-20251001 --max-iter 2

.DEFAULT_GOAL := help
