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

# Node LTS + the Claude Code CLI, pinned. The agent binary is a dominant
# non-model driver of eval results, so it travels with the coder_eval release
# tag and is bumped deliberately -- mirrors the codex CLI pin
# (`openai-codex-cli-bin==…` in pyproject). Leaving it at `@latest` let Docker
# layer caching freeze it nondeterministically across rebuilds.
ARG CLAUDE_CODE_VERSION=2.1.177
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# uv: matches host sandbox.py's `uv venv` + `uv pip install` fast path
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

WORKDIR /opt/coder_eval

# Install the coder_eval package itself. Build context expects to be the
# repo root; we copy the install-relevant subset to maximize cache hits.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# experiments/default.yaml is force-included by hatchling per pyproject.toml.
COPY experiments/default.yaml ./experiments/default.yaml

# Codex, Antigravity, and litellm are always baked into the image -- codex/
# antigravity are peers to the claude-code agent installed above, so all
# built-in agents ship in every build; litellm backs the `checker_context.
# api_route.route: litellm` judge dispatch (see pyproject.toml's `litellm`
# extra), which every DockerRunner-isolated task needs available IN-container
# since the checker runs there too, not just on the host. `openai-codex`
# (+ its pinned cli-bin), `google-antigravity` (which bundles its
# `localharness` binary as a manylinux wheel), and `litellm` all come from
# public PyPI, so this needs no private-index credentials. The RUN below
# always passes `--extra codex --extra antigravity --extra litellm`.
#
# CODER_EVAL_UV_EXTRAS carries ADDITIONAL opt-in extras on top of those; it
# defaults to none. `make docker-image-full` passes `--extra uipath`, which
# pulls the `uipath` SDK from public PyPI (no credentials required).
ARG CODER_EVAL_UV_EXTRAS=""

# safe-chain min-age exclusions for the codex cli-bin pin (see pyproject [tool.uv])
# and the pinned Antigravity harness. Codex and Antigravity are always installed,
# so the default excludes their pinned packages from the min-age gate (the
# google-antigravity release tracks the rapidly-moving Gemini harness and may be
# newer than the gate window); callers may override.
ARG SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS="openai-codex-cli-bin,openai-codex,google-antigravity"

# All extras (codex, antigravity, litellm, and the opt-in uipath) resolve from
# public PyPI per uv.lock, so the build needs no private-index credentials.
RUN export SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS="${SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS}" && \
    uv export --frozen --extra codex --extra antigravity --extra litellm ${CODER_EVAL_UV_EXTRAS} | uv pip install --system -r /dev/stdin

# Sanity check: the in-container entrypoint subcommand must be wired up.
RUN coder-eval _run-task-internal --help > /dev/null

# Stamp the image with the installed coder_eval version so the host can do
# a pre-flight version assertion against `docker image inspect` BEFORE
# spawning the container. Without this the host can only detect drift
# AFTER the (billed) run completes via the version field in task.json.
ARG CODER_EVAL_VERSION=unknown
LABEL org.coder-eval.version="${CODER_EVAL_VERSION}"
# Stamp the pinned agent binary too, so the host can assert it via
# `docker image inspect` before a (billed) run — same rationale as above.
LABEL org.coder-eval.claude-code-version="${CLAUDE_CODE_VERSION}"

WORKDIR /work
# Coder-eval-specific filename so it never collides with a base image's own
# /usr/local/bin/entrypoint.sh. Deliberately NO `ENTRYPOINT` is baked: the host
# pins it at run time via `docker run --entrypoint
# /usr/local/bin/coder_eval_entrypoint.sh`, which makes the in-container
# orchestrator launch robust to whatever a task image's Dockerfile declares.
COPY docker/coder_eval_entrypoint.sh /usr/local/bin/coder_eval_entrypoint.sh
RUN chmod +x /usr/local/bin/coder_eval_entrypoint.sh
