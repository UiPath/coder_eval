# Contributing to coder_eval

Thanks for your interest in contributing! This document covers how to set up your
environment, the quality bar every change must clear, and how to submit changes.

By participating in this project you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting Security Issues

Do **not** open a public issue for security vulnerabilities. See
[SECURITY.md](SECURITY.md) for the private disclosure process.

## Getting Started

Prerequisites:

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** 0.8+ for dependency management

```bash
git clone https://github.com/UiPath/coder_eval.git
cd coder_eval

# Installs dev + the optional [uipath] extra (all from public PyPI) AND the
# git pre-commit hooks. No credentials or private index required.
make install

# Or, to install the base dev environment and hooks manually:
uv sync --extra dev
uv run pre-commit install
```

`make install` (or the manual `pre-commit install`) wires up the git hooks that
run `ruff` and other checks on commit. See the [README](README.md) for the full
installation guide, including optional extras.

## Development Workflow

All checks are wired through the `Makefile`. Run the full suite before pushing —
it is the CI equivalent:

```bash
make verify   # format + lint + typecheck + test + custom lint + coverage
```

Individual targets:

| Command          | What it does                              |
|------------------|-------------------------------------------|
| `make format`    | `ruff format`                             |
| `make check`     | `ruff check` (lint)                       |
| `make typecheck` | `pyright`                                 |
| `make test`      | `pytest` (excludes live + custom-lint tests) |
| `make lint`      | custom architectural lint rules (CE001–)  |

Coverage is enforced at **80%** in CI.

## Coding Guidelines

- Read [CLAUDE.md](CLAUDE.md) — it documents the architecture, key patterns, and
  conventions (discriminated unions, the criterion plugin registry, the agent
  plugin SPI, the declarative merge resolver, etc.).
- **Import all core models from `coder_eval.models`**, never from submodules.
- New success criteria and agents plug in via the registry/SPI — see the
  "Extension Points" section of `CLAUDE.md`.
- When fixing a bug, ask whether a **custom lint rule** (`tests/lint/rules/`)
  could prevent the class of bug from recurring, and add one if so.
- Keep changes focused: no dead code, all imports used, all tests passing.

## Commit Messages

This repo uses **[Conventional Commits](https://www.conventionalcommits.org/)**
(enforced by CI). Format:

```
<type>(<optional scope>): <description>

feat: add JMESPath assertions to json_check
fix(docker): copy ~/.claude symlinks verbatim
docs: add community-health files
refactor!: remove the LLM Gateway proxy backend
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`. A `!`
after the type/scope (or a `BREAKING CHANGE:` footer) marks a breaking change.

## Pull Requests

1. Fork the repo (or branch, if you have write access) and create a topic branch.
2. Make your change, add/update tests, and run `make verify`.
3. Open a PR against `main` and fill out the PR template.
4. CI (`pr-checks`, conventional-commit lint, CodeQL) must pass, and at least one
   maintainer must approve.

Keep PRs small and single-purpose where possible — it makes review faster and
bisection easier.

### CI runners

Workflows run on UiPath's centralized managed GitHub pool, whose labels are listed in
[`.github/actionlint.yaml`](.github/actionlint.yaml). Nothing enforces that list, so
check a new label against it by hand — an unknown label is not a build error, the job
just queues until GitHub cancels it.

That pool enforces a minimum package-age safe-chain check on installs, so jobs that
install dependencies set `SAFE_CHAIN_MINIMUM_PACKAGE_AGE_EXCLUSIONS`. The literal in
that expression is the operative value — no secret of that name exists at repo or org
level. `pr-checks.yml` sets it once at the **workflow** level; don't add per-job copies.

Some jobs deliberately use stock `ubuntu-latest`, each explained at its `runs-on:`:
jobs that execute PR-supplied code (`quality-gate`, `no-uipath-extra`, `evalboard`)
fall back to it **for fork PRs only**, since this repo is public and untrusted code
should not run on the shared pool image — any new job running PR-supplied code needs
the same carve-out. `action-dogfood` always uses it, because it is the executable proof
behind the published Action and must exercise the image integrators actually use.

## Adding Tasks or Criteria

- Task YAMLs live in `tasks/`; see
  [docs/TASK_DEFINITION_GUIDE.md](docs/TASK_DEFINITION_GUIDE.md).
- Tests should use **Haiku or at most Sonnet** for any model calls — never Opus
  (cost).

## Releasing

Merges to `main` do **not** release. Dispatch the **Release** workflow from the
Actions tab and pick a bump level. It runs three jobs, in this order:

| Job | Does | Re-runnable? |
|-----|------|--------------|
| `release` | bumps the version, bumps `action.yml`'s `version:` pin, tags `vX.Y.Z`, pushes `main` + that tag, builds the wheel/sdist, pushes the GHCR agent image | **No** — re-running bumps and tags a *second* version |
| `publish-pypi` | publishes the wheel/sdist to public PyPI (OIDC, `pypi` environment) and asserts PyPI serves the exact files this run built | Yes |
| `promote` | moves the `v0` tag and cuts the GitHub Release | Yes |

`v0` is the ref every consumer pins (`uses: UiPath/coder_eval@v0`), and the
composite action installs `coder-eval==<action.yml's pin>`. So **nothing
consumer-visible moves until the wheel is on PyPI** — that is why the tag move and
the Release live in `promote` rather than in `release`.

### When a release goes red

Recover by re-running the failed jobs, never the whole workflow (`release` is not
re-runnable). `v0` keeps pointing at the last fully-published release throughout.

- **`publish-pypi` failed or is waiting on the `pypi` environment approval** —
  `vX.Y.Z` and `main` reference a version not yet on PyPI, but `@v0` consumers are
  healthy on the previous release. Re-run `publish-pypi`, then `promote`.
- **`promote` failed** — the wheel is published but `v0` still promises the previous
  version. Re-run `promote`.
- **`promote` refuses with "Refusing to move v0 backwards"** — you are re-running an
  *older* release's promote (GitHub keeps re-run available for 30 days). Promote the
  newest tag instead; the guard exists because a force-move would downgrade every
  consumer.
- **"Published artifact is not ours"** — PyPI serves files this run did not build.
  Do not promote; investigate before `v0` points consumers at them.

### The nightly gate

**Verify Published Action** (`.github/workflows/verify-published-action.yml`) runs
after every Release, on a daily cron, and on demand. Tier 1 is free and
deterministic; tier 2 spends a few cents driving the published action as a stranger
would. Annotations it emits, and what each means:

| Annotation | Meaning |
|---|---|
| `Stranded action.yml pin` | `@v0` promises a version PyPI does not have. Consumers are broken **now**. Re-run `publish-pypi`, then `promote`. |
| `promote did not run` | the newest version is published but `v0` still promises the previous one. Re-run `promote`. |
| `Release incomplete` (warning) | newest version tagged but unpublished; `@v0` consumers are fine. Finish the release. |
| `PyPI check inconclusive` / `Marketplace check inconclusive` / `Lag classification inconclusive` | an upstream transient or throttle, not a verdict. Re-run; do **not** re-publish on the strength of it. |
| `Marketplace listing missing` | the listing was renamed or delisted, or `action.yml`'s `name:` changed without it following. |
| `no tokens consumed … wiring is broken` | the published action never reached the model — credentials, agent runtime, or backend. |
| `harness/environment error` | a task failed for a non-model reason (install, sandbox, config). |

There is no notification path: a red nightly appears only in the Actions tab.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE), the same license that covers this project.
