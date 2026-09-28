"""``coder_eval.harbor`` — the Harbor framework adherence layer.

Harbor (Laude Institute / Terminal-Bench 2.0) is an outer harness with its own
runtime contract: a fixed reward-file convention, a fixed log layout, a trajectory
format. This package translates coder-eval's own artifacts into that contract. It
does not know how a task is *defined* — that is the export/packager concern.

A core layer like ``orchestration/``: it must not import ``coder_eval.cli`` (CE004).
Raise plain exceptions and let the CLI wrap them.

Rationale: .claude/notes/reporting.md § Harbor export
"""

from __future__ import annotations
