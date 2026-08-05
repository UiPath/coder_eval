"""The two derived version pins must equal ``pyproject.toml``'s version.

``pyproject.toml`` is the single version source; two files carry a *derived* pin
of it, and one ``release.yml`` step bumps both inside the release commit:

- ``action.yml``'s ``version:`` default — the published composite action installs
  ``coder-eval==<that default>``, so a consumer pinning
  ``UiPath/coder_eval@vX.Y.Z`` (or the moving ``@v0``) must get X.Y.Z and not
  some other release.
- ``plugins/coder-eval/.claude-plugin/plugin.json``'s ``version`` — the Claude
  Code plugin manifest. ``claude plugin validate --strict`` rejects a manifest
  with no version, and a pinned-but-stale one strands users on a cached copy
  because Claude Code keys plugin updates off it.

The release-time seds make both invariants mechanically true *at rest* — every
commit on main has the pins in agreement with ``pyproject.toml``.

Nothing asserted it, which is how ``action.yml`` shipped pinned to 0.8.6 while
main was already 0.8.9: the seds live on the release path only, so a hand-edit
(or a release whose amend step was skipped) drifts silently and ``@v0``
consumers install a version other than the tag they pinned.

This also guards the ``# <-- kept in sync`` anchor itself: ``release.yml``'s sed
is keyed on that exact trailing comment, so a reformat that detaches it turns
the release-time bump into a no-op (caught there by a ``grep -q`` guard, but
only after the tag exists).
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_YML = REPO_ROOT / "action.yml"
PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "coder-eval" / ".claude-plugin" / "plugin.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Mirrors the anchor release.yml's sed matches: indentation-tolerant, keyed on the
# unique trailing comment.
_PIN_PATTERN = re.compile(r'^[ \t]*default: "(?P<version>\d+\.\d+\.\d+)"[ \t]+# <-- kept in sync', re.MULTILINE)

# Likewise for plugin.json: release.yml's sed matches a whole line of this shape,
# INCLUDING the trailing comma. A reformat that moves `version` to the last key of
# the object (no comma) or collapses the JSON onto one line makes the bump a silent
# no-op on the release path — caught there by a `grep -q` guard, but only once the
# tag exists. Asserting the shape here moves that failure to every commit.
_PLUGIN_PIN_PATTERN = re.compile(r'^[ \t]*"version": "(?P<version>\d+\.\d+\.\d+)",[ \t]*$', re.MULTILINE)


def _project_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_action_version_pin_anchor_is_present_and_unique():
    """release.yml's sed is keyed on this anchor; a detached/duplicated one breaks it."""
    matches = _PIN_PATTERN.findall(ACTION_YML.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"expected exactly one '# <-- kept in sync' version pin in action.yml, found {len(matches)}; "
        "release.yml's sed anchor is keyed on it"
    )


def test_action_version_pin_matches_pyproject_version():
    match = _PIN_PATTERN.search(ACTION_YML.read_text(encoding="utf-8"))
    assert match is not None, "version pin anchor missing from action.yml"
    pinned = match.group("version")
    expected = _project_version()
    assert pinned == expected, (
        f"action.yml pins coder-eval=={pinned} but pyproject.toml is {expected}. "
        f"Consumers of UiPath/coder_eval@v{expected} would install {pinned}. "
        "Update the `default:` in action.yml (release.yml bumps it automatically on release)."
    )


def test_plugin_manifest_version_pin_anchor_is_present_and_unique():
    """release.yml's sed is keyed on this line shape; a reformat makes the bump a no-op."""
    matches = _PLUGIN_PIN_PATTERN.findall(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f'expected exactly one `"version": "X.Y.Z",` line (trailing comma included) in '
        f"{PLUGIN_MANIFEST.name}, found {len(matches)}; release.yml's sed anchor is keyed on it, "
        "so keep `version` off the last line of the object"
    )


def test_plugin_manifest_version_matches_pyproject_version():
    # Read through json, not the anchor regex: the VALUE invariant must hold whatever
    # the formatting is. The anchor test above owns the formatting half.
    pinned = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))["version"]
    expected = _project_version()
    assert pinned == expected, (
        f"{PLUGIN_MANIFEST.name} declares version {pinned} but pyproject.toml is {expected}. "
        "Claude Code keys plugin updates off this version, so a stale pin strands installed "
        "users on a cached copy. Update it (release.yml bumps it automatically on release)."
    )
