"""Emit a pyright config that type-checks the `tests/` modules worth checking.

`make typecheck`'s main pass cannot reach those modules. `pyproject.toml`'s
`[tool.pyright]` excludes `"tests"`, and pyright's `exclude` beats both an
explicitly-passed CLI file arg and an `include` entry naming the file: either
shortcut analyzes zero files and exits 0.

So the second pass needs its own config, DERIVED from `[tool.pyright]`: every
rule setting is copied verbatim, and only `include`, `exclude` (minus `"tests"`)
and `extraPaths` change, so the two passes cannot drift.

The pass covers CE036's contract engine and every `tests/*_live.py`. Live tests
need credentials to run, so nobody runs them on a routine change; checking their
types statically makes an SPI signature change fail the build instead.

Usage: `python -m tests.lint.pyright_config <output-path>`
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

INCLUDE = [
    "tests/lint/live_verdict_contract.py",
    "tests/_fixtures/live_criteria.py",
]

BYOA_DEMO_DIR = "tests/fixtures/byoa_demo_plugin"
BYOA_DEMO = f"{BYOA_DEMO_DIR}/byoa_demo.py"


def live_tests() -> list[str]:
    return sorted(path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / "tests").glob("*_live.py"))


def build_config() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        settings = dict(tomllib.load(handle)["tool"]["pyright"])

    settings["include"] = [*INCLUDE, *live_tests(), BYOA_DEMO]
    settings["exclude"] = [pattern for pattern in settings.get("exclude", []) if pattern != "tests"]
    settings["extraPaths"] = [*settings.get("extraPaths", []), BYOA_DEMO_DIR]
    return settings


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} <output-path>")
    out = Path(sys.argv[1]).resolve()
    if out.parent != REPO_ROOT:
        # Every path in the config stays relative, exactly as authored in
        # pyproject.toml. pyright resolves those (and the root for `tests.*` import
        # resolution) against the CONFIG FILE's directory, so the file has to sit at
        # the repo root to mean the same thing the main pass does.
        raise SystemExit(f"output must be written to the repo root ({REPO_ROOT}), got {out.parent}")
    out.write_text(json.dumps(build_config(), indent=2) + "\n")


if __name__ == "__main__":
    main()
