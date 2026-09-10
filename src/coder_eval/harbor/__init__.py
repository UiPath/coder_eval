"""``coder_eval.harbor`` — the Harbor framework adherence layer.

Harbor (Laude Institute / Terminal-Bench 2.0, harborframework.com) is an outer
harness with its own runtime contract: a fixed reward-file convention the
verifier phase must satisfy, a fixed log layout, a trajectory format. This
package is deliberately narrow — it translates coder-eval's own artifacts
(``task.json``, ``weighted_score``) into that contract. It does not know how a
task is *defined*; that is the export/packager concern (``coder-eval export
--format harbor``), tracked separately.

Direction-agnostic by design: the same shim serves a coder-eval task exported
to run under Harbor (coder-eval as the grader) and a coder-eval agent embedded
inside a Harbor-authored task (Harbor's own ``tests/test.sh`` as the grader,
Part A step 8 — coder-eval is purely the agent there and this package is
unused on that path).

This package is a core layer like ``orchestration/`` — it must not import
``coder_eval.cli`` (CE004). Raise plain exceptions and let the CLI wrap them,
exactly as ``orchestration/regrade.py`` does.
"""

from __future__ import annotations
