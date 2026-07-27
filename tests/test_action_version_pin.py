"""``action.yml``'s ``version:`` default must equal ``pyproject.toml``'s version.

The published composite action installs ``coder-eval==<that default>``, so a
consumer pinning ``UiPath/coder_eval@vX.Y.Z`` (or the moving ``@v0``) must get
X.Y.Z and not some other release. ``release.yml``'s "Regenerate uv.lock, bump
action.yml pin, and amend release commit" step sed-bumps the default inside the
release commit, which makes the invariant mechanically true *at rest* — every
commit on main has the two in agreement.

Nothing asserted it, which is how ``action.yml`` shipped pinned to 0.8.6 while
main was already 0.8.9: the sed lives on the release path only, so a hand-edit
(or a release whose amend step was skipped) drifts silently and ``@v0``
consumers install a version other than the tag they pinned.

This also guards the ``# <-- kept in sync`` anchor itself: ``release.yml``'s sed
is keyed on that exact trailing comment, so a reformat that detaches it turns
the release-time bump into a no-op (caught there by a ``grep -q`` guard, but
only after the tag exists).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_YML = REPO_ROOT / "action.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Mirrors the anchor release.yml's sed matches: indentation-tolerant, keyed on the
# unique trailing comment.
_PIN_PATTERN = re.compile(r'^[ \t]*default: "(?P<version>\d+\.\d+\.\d+)"[ \t]+# <-- kept in sync', re.MULTILINE)


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
