"""CE044 — the marketplace entry and the plugin manifest are one metadata surface.

``.claude-plugin/marketplace.json`` is what the ``/plugin`` browser shows before
install; ``plugins/coder-eval/.claude-plugin/plugin.json`` is what an installed copy
carries. For each marketplace entry:

- its ``source`` must resolve to a directory that holds a plugin manifest;
- every key in ``SHARED_KEYS`` must be equal on both sides;
- any other key on the entry fails unless ``MARKETPLACE_ONLY`` lists it with a
  reason. The allowlist is the rule: a new key needs a written reason.

Not a ``BaseRule``: it reasons over whole JSON files, and is wired as a
``@pytest.mark.lint`` class in ``tests/test_custom_lint.py``.

Rationale: .claude/notes/lint-rules.md § CE044
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Fields both manifests can express, and which mean the same thing on both sides.
# `version` is deliberately absent: only `plugin.json` carries it (a derived pin of
# pyproject, guarded by tests/test_action_version_pin.py), and an entry-side copy
# would be a third pin no release step maintains.
SHARED_KEYS = (
    "name",
    "displayName",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
)

# Keys the marketplace entry may carry that the plugin manifest has no counterpart
# for. Each needs a reason: adding one here is the deliberate act of saying "this
# term does not belong in the shared surface".
MARKETPLACE_ONLY: dict[str, str] = {
    "source": "the entry's pointer at the plugin directory; meaningless inside the manifest it points at",
    "category": "a marketplace-browser facet with a fixed vocabulary, not a free discovery string",
}


def _entry_source_dir(repo_root: Path, entry: dict[str, Any]) -> Path:
    source = entry.get("source")
    if not isinstance(source, str):
        raise TypeError(f"marketplace entry {entry.get('name')!r} has no string `source`")
    return (repo_root / source).resolve()


def check(repo_root: Path) -> list[str]:
    """Return one message per parity violation; empty means clean."""
    findings: list[str] = []
    marketplace_path = repo_root / ".claude-plugin" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))

    for entry in marketplace.get("plugins", []):
        name = entry.get("name")
        manifest_path = _entry_source_dir(repo_root, entry) / ".claude-plugin" / "plugin.json"
        if not manifest_path.is_file():
            findings.append(
                f"marketplace entry {name!r}: `source` does not resolve to a plugin manifest ({manifest_path})"
            )
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        for key in SHARED_KEYS:
            in_entry, in_manifest = entry.get(key), manifest.get(key)
            if in_entry != in_manifest:
                findings.append(
                    f"marketplace entry {name!r}: `{key}` differs from its plugin manifest\n"
                    f"      marketplace: {in_entry!r}\n"
                    f"      plugin.json: {in_manifest!r}"
                )

        extras = set(entry) - set(SHARED_KEYS) - set(MARKETPLACE_ONLY) - {"$schema"}
        for key in sorted(extras):
            findings.append(
                f"marketplace entry {name!r}: `{key}` has no counterpart in the plugin manifest, so its "
                f"value is dropped from an installed user's copy. Fold it into `keywords`, or add it to "
                f"MARKETPLACE_ONLY in tests/lint/plugin_manifest_parity.py with a reason."
            )

    return findings
