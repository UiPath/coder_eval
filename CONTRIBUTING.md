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

## Adding Tasks or Criteria

- Task YAMLs live in `tasks/`; see
  [docs/TASK_DEFINITION_GUIDE.md](docs/TASK_DEFINITION_GUIDE.md).
- Tests should use **Haiku or at most Sonnet** for any model calls — never Opus
  (cost).

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE), the same license that covers this project.
