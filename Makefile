.PHONY: help install format check typecheck test verify clean

help:  ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install project with dev dependencies
	uv pip install -e ".[dev]"
	pre-commit install
	@echo "✅ Installation complete!"

format:  ## Auto-format code with ruff
	uv run ruff format coder_eval/ tests/

check:  ## Run linting checks
	uv run ruff check coder_eval/ tests/

typecheck:  ## Run type checking with pyright
	uv run pyright

test:  ## Run test suite
	uv run pytest tests/ -v

test-cov:  ## Run tests with coverage report
	uv run pytest tests/ -v --cov=coder_eval --cov-report=term-missing --cov-report=html
	@echo "📊 Coverage report: htmlcov/index.html"

security:  ## Run security scans
	@echo "🔒 Running pip-audit..."
	uv run pip-audit --desc --skip-editable
	@echo "🔒 Running bandit..."
	uv run bandit -r coder_eval/ -ll

verify:  ## Run all verification steps (CI equivalent)
	@echo "🔍 Running format check..."
	@uv run ruff format --check coder_eval/ tests/
	@echo "🔍 Running lint check..."
	@uv run ruff check coder_eval/ tests/
	@echo "🔍 Running type check..."
	@uv run pyright
	@echo "🔍 Running tests with coverage..."
	@uv run pytest tests/ -v --cov=coder_eval --cov-fail-under=80
	@echo "✅ All verification checks passed!"

clean:  ## Clean build artifacts and cache
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache .coverage htmlcov/ bandit-report.json
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "🧹 Cleaned artifacts"

.DEFAULT_GOAL := help
