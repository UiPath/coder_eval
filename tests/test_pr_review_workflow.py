"""Mechanical guards for the untrusted-input ``claude-pr-review`` workflow.

This workflow runs privileged (write-scoped ``GITHUB_TOKEN``, repo secrets) over
attacker-controlled PR content, so three invariants are locked in here so a
future edit fails loudly instead of silently regressing the hardening:

1. ``include_comments_by_actor`` stays in sync with the CODEOWNERS ``*`` owners —
   the comment-source allowlist is a hand-maintained duplicate of that list, and
   drift silently drops a maintainer's review guidance from Claude's context.
2. The ``--allowedTools`` list contains no tool that can read on-disk secrets,
   re-ingest unfiltered untrusted content, or reach the network — e.g. a shell
   ``cat`` (reads a persisted ``.git/config`` token) or ``gh pr view`` (reads
   every comment verbatim, bypassing the actor allowlist).
3. ``persist-credentials: false`` removes the on-disk token the action's own
   ``git fetch`` needs, so auth is restored via an env-only credential helper.
   Both halves are locked in so the fetch path can't be re-broken (helper
   dropped → "could not read Username") nor the token re-persisted to disk (a
   literal ``secrets.*`` baked into the helper instead of an env reference).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-pr-review.yml"
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"

# Tool-allowlist entries that must never appear: each can read on-disk secrets,
# re-ingest unfiltered untrusted content, or reach the network from inside a
# privileged run over attacker-controlled input.
FORBIDDEN_TOOL_PATTERNS = (
    re.compile(r"Bash\(\s*cat\b"),  # cat .git/config → persisted token
    re.compile(r"Bash\(\s*ls\b"),  # redundant with Glob; broadens shell surface
    re.compile(r"Bash\(\s*gh\s+pr\s+view\b"),  # bypasses include_comments_by_actor
    re.compile(r"Bash\(\s*gh\s+api\b"),  # arbitrary GitHub API via the write token
    re.compile(r"Bash\(\s*gh\s+secret\b"),  # secret access
    re.compile(r"\bWebFetch\b"),  # network egress / exfil sink
    re.compile(r"\bWebSearch\b"),  # network egress
    re.compile(r"Bash\(\s*curl\b"),  # network egress / exfil sink
    re.compile(r"Bash\(\s*wget\b"),  # network egress
)


def _claude_review_step() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["claude-review"]["steps"]
    for step in steps:
        if step.get("uses", "").startswith("anthropics/claude-code-action@"):
            return step
    raise AssertionError("claude-code-action step not found in claude-pr-review.yml")


def _codeowners_default_owners() -> set[str]:
    """Return the ``*`` owner logins (without the leading ``@``)."""
    for raw in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        if pattern == "*":
            return {owner.lstrip("@") for owner in owners}
    raise AssertionError("no '*' default-owner line found in CODEOWNERS")


def test_include_comments_by_actor_matches_codeowners() -> None:
    """The comment-source allowlist must equal the CODEOWNERS ``*`` owners.

    Drift means a maintainer added to (or removed from) CODEOWNERS silently
    desyncs the reviewer's trusted-comment context. This fails loudly instead.
    """
    step = _claude_review_step()
    allowlist = {a.strip() for a in step["with"]["include_comments_by_actor"].split(",") if a.strip()}
    assert allowlist == _codeowners_default_owners(), (
        "include_comments_by_actor in claude-pr-review.yml has drifted from the "
        "CODEOWNERS '*' owners — keep the two lists in sync."
    )


def test_allowed_tools_have_no_secret_or_network_reach() -> None:
    """The ``--allowedTools`` list must stay read-only and network-free.

    Guards against a future edit re-adding a tool that can exfiltrate the
    write-scoped token or bypass the comment allowlist.
    """
    step = _claude_review_step()
    claude_args = step["with"]["claude_args"]
    match = re.search(r'--allowedTools\s+"([^"]*)"', claude_args)
    assert match, "could not locate --allowedTools in claude_args"
    allowed = match.group(1)
    offenders = [p.pattern for p in FORBIDDEN_TOOL_PATTERNS if p.search(allowed)]
    assert not offenders, f"claude-pr-review --allowedTools grants forbidden tools: {offenders}"


def test_checkout_does_not_persist_credentials() -> None:
    """The checkout step must set ``persist-credentials: false``.

    Default-true persistence writes the write-scoped GITHUB_TOKEN into
    .git/config on disk, where a prompt-injected read could exfiltrate it.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["claude-review"]["steps"]
    checkout = next((s for s in steps if s.get("uses", "").startswith("actions/checkout@")), None)
    assert checkout is not None, "checkout step not found in claude-pr-review.yml"
    assert checkout.get("with", {}).get("persist-credentials") is False, (
        "actions/checkout must set persist-credentials: false so the write-scoped "
        "GITHUB_TOKEN is not persisted to .git/config."
    )


def test_git_fetch_auth_is_env_only() -> None:
    """The action's internal ``git fetch`` must authenticate via an env-only helper.

    ``persist-credentials: false`` (above) removes the on-disk token that the
    claude-code-action's ``git fetch origin <branch>`` relies on, so a step must
    reconfigure git auth. This locks in the full linkage so a rename or reorder
    can't silently re-break the "could not read Username" regression:

    * a run-step configures a git credential helper (host-scoped ``credential.<url>.helper``);
    * the helper reads the token from an env var, never a baked-in ``secrets.*``
      literal (which would re-persist it to ~/.gitconfig and reopen the exfil surface);
    * the action step supplies *that exact* env var, sourced from
      ``secrets.GITHUB_TOKEN`` (not only the ``with.github_token`` input, which
      octokit uses but the raw ``git fetch`` does not);
    * the helper step runs *before* the action step (else the fetch precedes the config).
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["claude-review"]["steps"]

    helper_idx = next(
        (i for i, s in enumerate(steps) if re.search(r"credential\.\S*\.helper", s.get("run") or "")),
        None,
    )
    assert helper_idx is not None, (
        "with persist-credentials: false, a run-step must configure a git "
        "credential helper so the action's internal `git fetch` can authenticate."
    )
    helper_run = steps[helper_idx]["run"]
    assert "secrets." not in helper_run, (
        "the credential helper must reference the token via an env var, not embed "
        "a ${{ secrets.* }} literal (which would persist the token to ~/.gitconfig)."
    )

    # Couple the two halves: the env var the helper READS must be exactly the one
    # the action step SUPPLIES — else a rename on either side leaves `git fetch`
    # with an empty password while the independent existence checks still pass.
    match = re.search(r"password=\$\{?(\w+)\}?", helper_run)
    assert match, "credential helper must read the token from an env var (password=${VAR})"
    token_var = match.group(1)

    action_idx = next(
        (i for i, s in enumerate(steps) if s.get("uses", "").startswith("anthropics/claude-code-action@")),
        None,
    )
    assert action_idx is not None, "claude-code-action step not found in claude-pr-review.yml"
    action_env = steps[action_idx].get("env", {})
    assert token_var in action_env, (
        f"the credential helper reads ${token_var}, but the claude-code-action step's env "
        f"does not supply it — `git fetch` would get an empty password."
    )
    assert "secrets.GITHUB_TOKEN" in str(action_env[token_var]), (
        f"{token_var} must be sourced from secrets.GITHUB_TOKEN (env-only, never on disk)."
    )

    assert helper_idx < action_idx, (
        "the credential-helper step must run BEFORE the claude-code-action step, or the "
        "action's internal `git fetch` runs before ~/.gitconfig is configured and fails."
    )
