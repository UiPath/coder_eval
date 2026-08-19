"""Emit a pyright config that type-checks the CE036 contract engine under `tests/`.

`make typecheck`'s main pass cannot reach those modules. `pyproject.toml`'s
`[tool.pyright]` excludes `"tests"`, and pyright's `exclude` beats BOTH of the
obvious shortcuts (verified against a probe file carrying a deliberate error):

* `pyright tests/lint/live_verdict_contract.py` — an explicitly-passed CLI file
  arg is still excluded: `filesAnalyzed: 0`, exit 0. A gate that checks nothing.
* adding the path to `include` — likewise dropped; the probe never appears in
  the analyzed set.

So the second pass needs its own config. This script DERIVES it from
`[tool.pyright]` — every rule setting is copied verbatim, and only `include`
(the modules below) and `exclude` (minus `"tests"`) are swapped. That is the
point of generating it instead of checking in a hand-written twin: a rule tuned
in `pyproject.toml` applies to both passes, and the two can never drift.

Usage: `python -m tests.lint.pyright_config <output-path>`
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# The `tests/` modules worth type-checking: CE036's contract engine executes real
# checker code and encodes the early-stop design, so a type error there is a bug in
# the gate itself. Add a path here only for a tests/ module with that character —
# this is deliberately not "all of tests/".
INCLUDE = [
    "tests/lint/live_verdict_contract.py",
    "tests/_fixtures/live_criteria.py",
]


def build_config() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        settings = dict(tomllib.load(handle)["tool"]["pyright"])

    settings["include"] = list(INCLUDE)
    settings["exclude"] = [pattern for pattern in settings.get("exclude", []) if pattern != "tests"]
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
