"""The class-level paths and grader helpers the plugin-surface test classes share.

`TestPluginArtifacts` was 3,226 lines — a third of the lint monolith — and split into five
classes by the surface each one is about. Its four class attributes and four static helpers
were referenced through `self.`, so they move to a base every one of the five inherits
rather than being copied five times.
"""

from pathlib import Path

from tests.lint_tests.shared import PLUGIN_ROOT, REPO_ROOT


class PluginArtifactsBase:
    """The Claude Code plugin's shipped artifacts must be valid and self-contained."""

    REPO_ROOT = REPO_ROOT
    TEMPLATES = PLUGIN_ROOT / "reference" / "templates"
    GRADER = PLUGIN_ROOT / "reference" / "templates" / "outcome-grader" / "verify.py"

    @staticmethod
    def _grade(
        tmp_path: Path,
        spec: object,
        artifact: str | None,
        *,
        row: str = "r1",
        artifact_path: str | None = None,
    ) -> tuple[float, str, int]:
        """Run the shipped scaffold over one fabricated row, returning (score, output, exit code).

        Copies the scaffold rather than pointing at it in place: the grader resolves its
        expectations relative to ITSELF, so writing fixtures beside the real one would leave files
        in the plugin tree. The artifact is written under `cwd`, which is what `run_command` sets
        to the sandbox.
        """
        import json
        import shutil
        import subprocess
        import sys

        grader_dir = tmp_path / "grader"
        # `parents=True`: callers pass a SUBdirectory of the fixture when one test grades two
        # artifacts (the discrimination margin), and that parent has not been created.
        grader_dir.mkdir(parents=True)
        shutil.copy(PluginArtifactsBase.GRADER, grader_dir / "verify.py")
        (grader_dir / "expectations").mkdir()
        (grader_dir / "expectations" / f"{row}.json").write_text(json.dumps(spec), encoding="utf-8")

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        if artifact is not None:
            # `artifact_path` for the malformed-spec cases, where the spec cannot supply one.
            relative = artifact_path or (spec["path"] if isinstance(spec, dict) else None)
            assert isinstance(relative, str), "pass artifact_path when the spec carries no usable one"
            target = sandbox / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(artifact, encoding="utf-8")

        completed = subprocess.run(
            [sys.executable, str(grader_dir / "verify.py"), row],
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=60,
        )
        first = completed.stdout.splitlines()[0]
        return float(first), completed.stdout, completed.returncode

    @staticmethod
    def _shipped_expectations() -> dict[str, dict]:
        import json

        directory = PluginArtifactsBase.GRADER.parent / "expectations"
        specs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))}
        assert specs, f"GAP: no expectations files under {directory} — this whole group would pass vacuously"
        return specs

    @staticmethod
    def _shipped_row_ids() -> list[str]:
        import json

        rows = PluginArtifactsBase.TEMPLATES / "outcome-rows.jsonl"
        ids = [json.loads(line)["id"] for line in rows.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert ids, f"GAP: {rows} carries no rows — the parity assertions below would compare two empty sets"
        return ids

    @staticmethod
    def _rules_line(output: str) -> dict[str, str]:
        """The grader's rule attribution, parsed from the LAST `RULES ` line of its stdout.

        Scanned from the END for the prefix rather than taken as `splitlines()[-1]` blindly: that
        is exactly what a consumer does, because `run_command` wraps the grader's stdout in a
        details block that appends a `Stderr:` section after it.
        """
        import json

        line = next(ln for ln in reversed(output.splitlines()) if ln.startswith("RULES "))
        return json.loads(line[len("RULES ") :])

    _RETIRED_CLAIMS = (
        (
            "guardrails gate here, in the procedure",
            "the guardrails gate in `holm_promote` / `holm_promote_execution` — in the library, "
            "not in the skill's prose",
        ),
        (
            "stay advisory in the model",
            "a failing guardrail FORCES `promoted = False` on both tracks",
        ),
        (
            "the cost/latency guardrails stay advisory",
            "both tracks fold their guardrails into `promoted`",
        ),
    )
