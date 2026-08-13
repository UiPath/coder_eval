"""``verify-published-action.yml`` couples to things nothing else asserts.

The workflow cannot be exercised before merge — ``workflow_run`` and ``schedule`` only
fire from the default branch — so every coupling it makes to another file is a place
where a rename passes ``make verify`` green and the gate silently rots in production.
Four such couplings, each with an executable binding here:

1. **``workflow_run: workflows: ["Release"]``** matches ``release.yml``'s ``name:`` by
   display string. GitHub does not error on an unmatched name; the trigger simply never
   fires, degrading the gate to schedule-only with no signal.
2. **The Marketplace slug** is derived by a shell pipeline, a *second* slugger next to
   the tested ``tests/lint/action_docs.py::marketplace_slug`` that CE026 uses for the doc
   links. They agree today only because ``action.yml``'s ``name:`` is ``coder_eval`` — the
   one input for which both are the identity function.
3. **The ``# <-- kept in sync`` pin anchor** now has three readers with three different
   whitespace tolerances (``release.yml``'s sed, this workflow's sed, and
   ``tests/test_action_version_pin.py``). A reformat can leave one reporting "parity OK"
   on a pin another silently refused to bump.
4. **The inline consumer task YAML** is a whole ``TaskDefinition`` document that no test
   validates, while CE029 already validates that exact shape in Markdown. Any field
   rename (or an ``extra="forbid"`` violation) would surface only as an opaque failure in
   the paid nightly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.lint.action_docs import action_listing_name, marketplace_slug


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
VERIFY_WF = WORKFLOWS / "verify-published-action.yml"
RELEASE_WF = WORKFLOWS / "release.yml"
ACTION_YML = REPO_ROOT / "action.yml"


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} did not parse as a mapping"
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """The ``on:`` block. PyYAML resolves the bare key ``on`` to the boolean ``True``."""
    block = workflow.get("on", workflow.get(True))
    assert isinstance(block, dict), "workflow has no parseable `on:` block"
    return block


def _run_body(workflow: dict[str, Any], step_name: str) -> str:
    """The ``run:`` script of a named step, already dedented by the YAML parser."""
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("name") == step_name:
                body = step.get("run")
                assert isinstance(body, str), f"step '{step_name}' has no `run:` body"
                return body
    raise AssertionError(f"no step named '{step_name}'")


def _line_after(body: str, anchor: str) -> str:
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if anchor in line:
            assert i + 1 < len(lines), f"anchor '{anchor}' is the last line of the step"
            return lines[i + 1].strip()
    raise AssertionError(f"anchor '{anchor}' not found — did the step get reflowed?")


def _bash_result(
    script: str,
    stdin: str = "",
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a lifted snippet and return the raw result, exit code included.

    Used for the guards, whose whole contract is *refusing* — asserting exit 0 would
    make every one of them untestable.
    """
    return subprocess.run(
        ["bash", "-c", script],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **(env or {})},
        cwd=cwd,
        check=False,
    )


def _bash(script: str, stdin: str = "", env: dict[str, str] | None = None) -> str:
    """Run a snippet lifted verbatim out of a workflow. Inputs go through the
    environment, never argv or interpolation, so a fixture value carrying quotes cannot
    be mistaken for shell syntax."""
    proc = _bash_result(script, stdin=stdin, env=env)
    assert proc.returncode == 0, f"script failed ({proc.returncode}): {proc.stderr}"
    return proc.stdout


def _body_before(body: str, marker: str) -> str:
    """The lines of a ``run:`` body up to (excluding) the first line starting with
    ``marker``. Lets a guard be exercised without the irreversible action it guards."""
    lines = body.splitlines()
    cut = next((i for i, line in enumerate(lines) if line.strip().startswith(marker)), None)
    assert cut is not None, f"marker {marker!r} not found — did the step get restructured?"
    return "\n".join(lines[:cut])


def _slug_pipeline() -> str:
    """The one-line slug derivation in the preflight job, lifted from its anchor."""
    body = _run_body(_load(VERIFY_WF), "Verify Marketplace listing resolves")
    pipeline = _line_after(body, "slug-derivation-anchor")
    assert pipeline.startswith("SLUG="), f"unexpected line under the anchor: {pipeline!r}"
    return pipeline


# --------------------------------------------------------------------------------------
# 1. workflow_run couples to release.yml's display name
# --------------------------------------------------------------------------------------


def test_workflow_run_names_the_real_release_workflow():
    named = _triggers(_load(VERIFY_WF))["workflow_run"]["workflows"]
    release_name = _load(RELEASE_WF)["name"]
    assert named == [release_name], (
        f"verify-published-action.yml triggers on workflows {named}, but release.yml is named "
        f"'{release_name}'. GitHub does not error on an unmatched name — the trigger just never "
        "fires, so release-time verification degrades to the nightly cron with no signal."
    )


# --------------------------------------------------------------------------------------
# 2. one Marketplace slugger
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "listing_name",
    [
        "coder_eval",  # today's value: the one input where every slugger agrees
        "Coder Eval (CI gate)",  # punctuation the naive `tr ' ' '-'` pipeline kept
        "  Coder   Eval  ",  # leading/trailing space + a whitespace run
        "coder_eval v2.0",  # a dot, which is legal in a slug
    ],
)
def test_workflow_slug_pipeline_matches_the_tested_slugger(listing_name: str):
    got = _bash(
        f'NAME="$LISTING_NAME"; {_slug_pipeline()}; printf "%s" "$SLUG"',
        env={"LISTING_NAME": listing_name},
    )
    assert got == marketplace_slug(listing_name), (
        f"the workflow's slug pipeline yields {got!r} for {listing_name!r} but "
        f"marketplace_slug() (which CE026 pins the doc links to) yields "
        f"{marketplace_slug(listing_name)!r}"
    )


def test_action_listing_name_slugs_identically_in_both_implementations():
    """The live value, end to end: whatever `action.yml` declares today."""
    name = action_listing_name(ACTION_YML)
    got = _bash(
        f'NAME="$LISTING_NAME"; {_slug_pipeline()}; printf "%s" "$SLUG"',
        env={"LISTING_NAME": name},
    )
    assert got == marketplace_slug(name)


# --------------------------------------------------------------------------------------
# 3. the `# <-- kept in sync` pin anchor has three readers
# --------------------------------------------------------------------------------------


def test_all_three_pin_anchor_readers_agree_on_action_yml():
    """release.yml's sed (bump), this workflow's sed (read), and the unit test's regex."""
    from tests.test_action_version_pin import _PIN_PATTERN

    action_text = ACTION_YML.read_text(encoding="utf-8")
    expected = _PIN_PATTERN.search(action_text)
    assert expected is not None, "the pin anchor regex no longer matches action.yml"

    # (a) The workflow's EXTRACTING sed, applied to action.yml exactly as the preflight
    #     job applies it to `git show v0:action.yml`.
    read_sed = next(
        line.strip()
        for line in _run_body(_load(VERIFY_WF), "Check tag / pin parity").splitlines()
        if line.strip().startswith("| sed -nE") and "kept in sync" in line
    ).lstrip("| ")
    # The sed closes the `PIN=$(git show … | sed …)` substitution the parity step opens.
    read_sed = read_sed.removesuffix(")")
    read = _bash(read_sed, stdin=action_text).strip()
    assert read == expected.group("version"), (
        f"the workflow's sed reads the pin as {read!r} but the anchor regex reads {expected.group('version')!r}"
    )

    # (b) release.yml's BUMPING sed. `-i` and the filename are dropped so the expression
    #     is exercised portably over stdin (BSD sed's `-i` takes a suffix argument).
    bump_sed = next(
        line.strip()
        for line in _run_body(
            _load(RELEASE_WF), "Regenerate uv.lock, bump action.yml + plugin.json pins, and amend release commit"
        ).splitlines()
        if line.strip().startswith("sed -i -E") and "kept in sync" in line
    )
    bump_sed = bump_sed.replace("sed -i -E", "sed -E").removesuffix(" action.yml")
    bumped = _bash(f"VERSION=9.9.9; {bump_sed}", stdin=action_text)
    assert 'default: "9.9.9"' in bumped, (
        "release.yml's sed did not match the pin anchor in action.yml, so a release would "
        "ship a stale `version:` default (its own grep guard would fail the release)"
    )


# --------------------------------------------------------------------------------------
# 4. the inline consumer task YAML is a real TaskDefinition
# --------------------------------------------------------------------------------------


def test_inline_consumer_task_yaml_loads(tmp_path: Path):
    from coder_eval.orchestration.task_loader import load_task

    body = _run_body(_load(VERIFY_WF), "Write a consumer task YAML")
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'YAML'" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "YAML")
    task_yaml = "\n".join(lines[start + 1 : end]) + "\n"

    path = tmp_path / "published_smoke.yaml"
    path.write_text(task_yaml, encoding="utf-8")
    task, _ = load_task(path)

    assert task.task_id == "published_action_smoke"
    assert task.success_criteria, "the nightly's task must assert something"


def test_inline_consumer_task_declares_run_limits(tmp_path: Path):
    """The nightly is unattended and paid: an uncapped run burns spend until the job's
    `timeout-minutes` cancels the runner, which produces no run.json for the gate to
    read. Every RunLimits cap defaults to None, so omitting the block is silent."""
    from coder_eval.orchestration.task_loader import load_task

    body = _run_body(_load(VERIFY_WF), "Write a consumer task YAML")
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'YAML'" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "YAML")
    path = tmp_path / "published_smoke.yaml"
    path.write_text("\n".join(lines[start + 1 : end]) + "\n", encoding="utf-8")

    limits = load_task(path)[0].run_limits
    assert limits is not None, "the unattended paid task must declare run_limits"
    assert limits.max_turns, "an unbounded turn count on a cron-triggered paid run"
    assert limits.max_usd, "an unbounded spend on a cron-triggered paid run"
    assert limits.task_timeout, "no wall-clock cap below the job's timeout-minutes"


# --------------------------------------------------------------------------------------
# 5. the promote job's two consumer-visible guards
#
# `promote` moves `v0` — the ref every `uses: UiPath/coder_eval@v0` consumer resolves.
# Its two guards are the branch's highest-stakes new shell and cannot be exercised
# before merge (promote only runs on a real release), so they are lifted and run here.
# --------------------------------------------------------------------------------------


def _version_shape_guard() -> str:
    return _run_body(_load(RELEASE_WF), "Validate version shape")


@pytest.mark.parametrize("version", ["0.9.6", "1.0.0", "10.20.30"])
def test_version_shape_guard_accepts_real_releases(version: str):
    proc = _bash_result(_version_shape_guard(), env={"VERSION": version})
    assert proc.returncode == 0, f"guard rejected the valid version {version!r}: {proc.stdout}{proc.stderr}"
    assert f"promoting v{version}" in proc.stdout


@pytest.mark.parametrize(
    "version",
    [
        "0.9",  # too few components
        "0.9.6.1",  # too many
        "0.9.6rc1",  # a prerelease is not promotable to the major tag
        "v0.9.6",  # already prefixed; would yield `vv0.9.6` in the ref
        "0.9.6 && echo pwned",  # shape check is defence-in-depth for the `ref:` interpolation
    ],
)
def test_version_shape_guard_refuses_malformed_versions(version: str):
    proc = _bash_result(_version_shape_guard(), env={"VERSION": version})
    assert proc.returncode == 1, f"guard accepted the malformed version {version!r}"
    assert "malformed version" in proc.stdout


def test_version_shape_guard_owns_the_empty_version_case():
    """The job's `if:` deliberately does not gate on the output being non-empty (that
    shape resolves to SKIPPED-green and strands a release), so this guard is the sole
    enforcement point — and it must say so, not just fail the regex."""
    proc = _bash_result(_version_shape_guard(), env={"VERSION": ""})
    assert proc.returncode == 1
    assert "Release version unavailable" in proc.stdout, (
        "an empty version must get its own partial-re-run diagnostic, not the generic "
        f"malformed-version message: {proc.stdout}"
    )


def _tag_repo(root: Path, tags: tuple[str, ...]) -> Path:
    """A git repo carrying `tags` on one commit, for the monotonicity guard to read."""
    run = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run([*run, "add", "f.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run([*run, "commit", "-qm", "c"], cwd=root, check=True, capture_output=True)
    for tag in tags:
        subprocess.run(["git", "tag", tag], cwd=root, check=True, capture_output=True)
    return root


def _monotonicity_guard() -> str:
    """The guard only — everything before the irreversible `git tag -f` / `git push -f`."""
    body = _run_body(_load(RELEASE_WF), "Move major action tag (vN -> this release)")
    guard = _body_before(body, "git tag -f")
    assert "NEWEST" in guard, "the monotonicity check is no longer above the tag move"
    return guard


def test_monotonicity_guard_allows_promoting_the_newest_release(tmp_path: Path):
    repo = _tag_repo(tmp_path / "newest", ("v0.9.5", "v0.9.6", "v0"))
    proc = _bash_result(_monotonicity_guard(), env={"VERSION": "0.9.6"}, cwd=repo)
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"


def test_monotonicity_guard_refuses_to_walk_the_major_tag_backwards(tmp_path: Path):
    """The 30-day "Re-run failed jobs" hazard: replaying an OLD release's promote after a
    newer one shipped would downgrade every consumer pinned to the major tag."""
    repo = _tag_repo(tmp_path / "backwards", ("v0.9.5", "v0.9.6", "v0"))
    proc = _bash_result(_monotonicity_guard(), env={"VERSION": "0.9.5"}, cwd=repo)
    assert proc.returncode == 1, "the guard promoted a superseded release"
    assert "backwards" in proc.stdout
    assert "v0.9.6" in proc.stdout, "the operator is not told which release to promote instead"


def test_monotonicity_guard_refuses_when_no_release_tag_exists(tmp_path: Path):
    """`v0` alone must not satisfy the newest-tag lookup: the grep keeps only vX.Y.Z, and
    an empty result has to fail loudly rather than compare against the empty string."""
    repo = _tag_repo(tmp_path / "majoronly", ("v0",))
    proc = _bash_result(_monotonicity_guard(), env={"VERSION": "0.9.6"}, cwd=repo)
    assert proc.returncode == 1
    assert "no vX.Y.Z release tag found" in proc.stdout
