.PHONY: help install format check typecheck test test-live test-smoke verify verify-noextra clean run lint docker-image

help:  ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install project with dev + uipath dependencies (hash-verified from uv.lock)
	# Includes the optional [uipath] extra so the dev install has parity with CI
	# (LLMGW judge transport, rephrase mutation, in-host uipath SDK). Requires
	# UV_INDEX_UIPATH_USERNAME / UV_INDEX_UIPATH_PASSWORD for the private feed —
	# see the install matrix in README.md if you want to skip the extra.
	uv sync --frozen --extra dev --extra uipath
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

verify-noextra:  ## Verify the framework works without the optional [uipath] extra
	# Build a throwaway venv that has ONLY the [dev] extra (no [uipath]); confirms
	# `pip install coder-eval` (without the extra) is functional. Mirrors the
	# `no-uipath-extra` CI job. Run as: `make verify-noextra` after `make install`.
	rm -rf .venv-noextra
	python3.13 -m venv .venv-noextra
	.venv-noextra/bin/pip install --upgrade pip uv >/dev/null
	.venv-noextra/bin/uv pip install --python .venv-noextra/bin/python -e ".[dev]"
	@echo "--- verifying uipath / uipath_llmgw_client are NOT installed ---"
	! .venv-noextra/bin/python -c "import uipath_llmgw_client" 2>/dev/null
	! .venv-noextra/bin/python -c "import uipath" 2>/dev/null
	@echo "--- running optional-dependency tests ---"
	.venv-noextra/bin/pytest tests/test_optional_dependencies.py tests/test_llm_gateway_helper.py tests/test_rephrase.py -v --no-header
	rm -rf .venv-noextra

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

docker-image:  ## Build the coder-eval-agent container image
	@VERSION=$$(uv run python -c "from importlib.metadata import version; print(version('coder-eval'))"); \
	echo "Building coder-eval-agent:$$VERSION"; \
	docker buildx build -t coder-eval-agent:$$VERSION -t coder-eval-agent:latest -f docker/Dockerfile \
		--build-arg CODER_EVAL_VERSION=$$VERSION \
		--build-arg SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS \
		--secret id=uv_index_username,env=UV_INDEX_UIPATH_USERNAME \
		--secret id=uv_index_password,env=UV_INDEX_UIPATH_PASSWORD \
		.

.DEFAULT_GOAL := help
