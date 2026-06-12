# coder-eval-agent: per-task evaluation container.
#
# Built once per coder_eval release (`make docker-image`). The host's
# DockerRunner spawns one container per task; the entrypoint runs the same
# Orchestrator/Sandbox/Agent code as host-process mode and writes task.json
# to a bind-mounted output dir.
#
# Credentials (ANTHROPIC_API_KEY etc.) are NEVER baked in -- the host passes
# them via -e at run time.

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# System deps: git for repo-source templates; curl/ca-certs for HTTPS;
# build-essential because some Python deps (pylint plugins) compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Node LTS + bun (matches host sandbox.py's `bun add` fast path)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@latest

# uv: matches host sandbox.py's `uv venv` + `uv pip install` fast path
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

WORKDIR /opt/coder_eval

# Install the coder_eval package itself. Build context expects to be the
# repo root; we copy the install-relevant subset to maximize cache hits.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# experiments/default.yaml is force-included by hatchling per pyproject.toml.
COPY experiments/default.yaml ./experiments/default.yaml

# Optional extras to install, expressed as `uv export` flags. Defaults to the
# agnostic core (no extras) -- mirroring `pip install coder-eval` -- so the image
# builds without private-index credentials. The UiPath + Codex extras are
# opt-in: `make docker-image-full` passes
# CODER_EVAL_UV_EXTRAS="--extra uipath --extra codex", which pulls
# uipath-llmgw-client from a private index (needs the secrets below). The index
# is only wired up when the uipath extra is requested.
ARG CODER_EVAL_UV_EXTRAS=""

# safe-chain min-age exclusions for the codex cli-bin pin (see pyproject [tool.uv]).
ARG SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS=""

# UiPath private index credentials are passed at build time via BuildKit secrets,
# NOT ARG/ENV -- the secret mount is only visible to this RUN, and nothing about
# it is recoverable from `docker history` or `docker save | tar`. The index is
# only wired up when the uipath extra is requested, so the lite build never
# queries (or needs credentials for) the private feed. See `make docker-image`.
RUN --mount=type=secret,id=uv_index_username \
    --mount=type=secret,id=uv_index_password \
    export SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS="${SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS}" && \
    case "${CODER_EVAL_UV_EXTRAS}" in \
      *uipath*) \
        export UV_INDEX_UIPATH_USERNAME=$(cat /run/secrets/uv_index_username 2>/dev/null || true) && \
        export UV_INDEX_UIPATH_PASSWORD=$(cat /run/secrets/uv_index_password 2>/dev/null || true) && \
        export UV_EXTRA_INDEX_URL="https://${UV_INDEX_UIPATH_USERNAME}:${UV_INDEX_UIPATH_PASSWORD}@uipath.pkgs.visualstudio.com/_packaging/ml-packages/pypi/simple/" ;; \
    esac && \
    uv export --frozen ${CODER_EVAL_UV_EXTRAS} | uv pip install --system -r /dev/stdin

# Sanity check: the in-container entrypoint subcommand must be wired up.
RUN coder-eval _run-task-internal --help > /dev/null

# Stamp the image with the installed coder_eval version so the host can do
# a pre-flight version assertion against `docker image inspect` BEFORE
# spawning the container. Without this the host can only detect drift
# AFTER the (billed) run completes via the version field in task.json.
ARG CODER_EVAL_VERSION=unknown
LABEL org.coder-eval.version="${CODER_EVAL_VERSION}"

WORKDIR /work
# Coder-eval-specific filename so it never collides with a base image's own
# /usr/local/bin/entrypoint.sh. Deliberately NO `ENTRYPOINT` is baked: the host
# pins it at run time via `docker run --entrypoint
# /usr/local/bin/coder_eval_entrypoint.sh`, which makes the in-container
# orchestrator launch robust to whatever a task image's Dockerfile declares.
COPY docker/coder_eval_entrypoint.sh /usr/local/bin/coder_eval_entrypoint.sh
RUN chmod +x /usr/local/bin/coder_eval_entrypoint.sh
