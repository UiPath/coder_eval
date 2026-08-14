.PHONY: help install format check typecheck test test-live test-smoke verify verify-noextra evalboard-verify clean run lint docs-indexes plugin-reference docker-image docker-image-full coder-eval-runtime docker-images

# Single source of the installed coder-eval version (used to tag the docker
# images). Referenced lazily inside the docker recipes, so it doesn't run on
# every `make` invocation (e.g. `make help`).
VERSION_CMD = uv run python -c "from importlib.metadata import version; print(version('coder-eval'))"

help:  ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install project with dev + uipath dependencies (hash-verified from uv.lock)
	# Includes the optional [uipath] extra for parity with CI (the in-host
	# `uipath` SDK, resolved from public PyPI). Drop `--extra uipath` to skip it.
	uv sync --frozen --extra dev --extra uipath
	uv run pre-commit install

# `.github/scripts/` is in scope on purpose: release tooling that lives in a real
# module (rather than inline in a workflow `run:` heredoc) is exactly what ruff,
# pyright and pytest can see — leaving it unlinted would forfeit the reason it was
# extracted.
LINT_PATHS := src/ tests/ .github/scripts/

format:  ## Auto-format code with ruff
	uv run ruff format $(LINT_PATHS)

check:  ## Run linting checks
	uv run ruff check $(LINT_PATHS)

lint:  ## Run custom architectural lint rules (CE001+)
	uv run pytest tests/test_custom_lint.py -v --tb=short --no-header -p no:warnings

docs-indexes:  ## Regenerate README/docs indexes from the mkdocs nav (SSOT)
	uv run python -m tests.lint.doc_indexes

plugin-reference:  ## Regenerate the plugin's bundled criteria reference from the models (SSOT)
	uv run python -m tests.lint.plugin_reference

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
	uv run ruff format --check $(LINT_PATHS)
	uv run ruff check $(LINT_PATHS)
	uv run pyright
	uv run pytest tests/test_custom_lint.py -v --tb=short --no-header -p no:warnings
	# uv run pip-audit --desc --skip-editable
	# uv run bandit -r src/ -ll --format json -o bandit-report.json
	uv run pytest tests/ -n auto -m "not live and not lint" --cov=coder_eval --cov-report=term-missing --cov-report=xml --cov-fail-under=80

evalboard-verify:  ## Run the evalboard (Next.js dashboard) checks: tsc + vitest + next build
	# The JS half of the repo. Not folded into `make verify` because it needs a
	# Node/pnpm toolchain a Python-only contributor may not have — but it IS
	# gated in CI by the `evalboard` job, so a red run here is a red PR.
	# Includes the pricing drift guard: a reprice in src/coder_eval/pricing.py
	# without the matching edit to evalboard/lib/pricing.ts fails right here.
	cd evalboard && pnpm install --frozen-lockfile && pnpm verify

verify-noextra:  ## Verify the framework works without the optional [uipath] extra
	# Build a throwaway venv that has ONLY the [dev] extra (no [uipath]); confirms
	# `pip install coder-eval` (without the extra) is functional. Mirrors the
	# `no-uipath-extra` CI job. Run as: `make verify-noextra` after `make install`.
	rm -rf .venv-noextra
	python3.13 -m venv .venv-noextra
	.venv-noextra/bin/pip install --upgrade pip uv >/dev/null
	.venv-noextra/bin/uv pip install --python .venv-noextra/bin/python -e ".[dev]"
	@echo "--- verifying uipath is NOT installed ---"
	! .venv-noextra/bin/python -c "import uipath" 2>/dev/null
	@echo "--- running optional-dependency tests ---"
	.venv-noextra/bin/pytest tests/test_optional_dependencies.py -v --no-header
	rm -rf .venv-noextra

clean:  ## Clean build artifacts and cache
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache .coverage coverage.xml htmlcov/ bandit-report.json coverage.xml
	rm -rf runs/2025-* runs/latest
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

run:	## Run coder-eval on all tasks with 8 parallel jobs
	uv run coder-eval run tasks/*.yaml tasks/agents/*.yaml -j 8

test-smoke:  ## Run e2e smoke tests with real API (mirrors CI "E2E Smoke Tests" job)
	uv run coder-eval run tasks/*.yaml --tags smoke-pass --model claude-haiku-4-5-20251001
	@echo "--- now running smoke-fail bucket (expected to exit non-zero) ---"
	! uv run coder-eval run tasks/*.yaml --tags smoke-fail --model claude-haiku-4-5-20251001

docker-image:  ## Build the coder-eval-agent image (core + both agents baked in; no creds needed)
	@VERSION=$$($(VERSION_CMD)); \
	echo "Building coder-eval-agent:$$VERSION (core + claude + codex)"; \
	DOCKER_BUILDKIT=1 docker build -t coder-eval-agent:$$VERSION -t coder-eval-agent:latest -f docker/Dockerfile \
		--build-arg CODER_EVAL_VERSION=$$VERSION \
		.

coder-eval-runtime:  ## Build the relocatable runtime kit image (COPY --from source for runtime-mode: inject)
	@VERSION=$$($(VERSION_CMD)); \
	echo "Building coder-eval-runtime:$$VERSION (relocatable kit: python+node+CLI under /opt/coder-eval)"; \
	DOCKER_BUILDKIT=1 docker build -t coder-eval-runtime:$$VERSION -t coder-eval-runtime:latest -f docker/Dockerfile.runtime \
		--build-arg CODER_EVAL_VERSION=$$VERSION \
		.

docker-images: docker-image coder-eval-runtime  ## Build BOTH base images (agent for rebase + runtime kit for inject); no creds
	@echo "Built coder-eval-agent + coder-eval-runtime — ready for both rebase and inject tasks."

docker-image-full:  ## Build with the UiPath extra (opt-in; uipath resolves from public PyPI, no credentials needed). Codex is always baked in.
	@VERSION=$$($(VERSION_CMD)); \
	echo "Building coder-eval-agent:$$VERSION (full: + uipath extra)"; \
	docker buildx build -t coder-eval-agent:$$VERSION -t coder-eval-agent:latest -f docker/Dockerfile \
		--build-arg CODER_EVAL_VERSION=$$VERSION \
		--build-arg CODER_EVAL_UV_EXTRAS="--extra uipath" \
		.

.DEFAULT_GOAL := help
