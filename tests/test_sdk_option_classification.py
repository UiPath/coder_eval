"""Regression: every ``ClaudeAgentOptions`` field must be classified.

The ``sdk_options`` pass-through validation in
:mod:`coder_eval.models.agent_config` allows any SDK field that is NOT in
``_FRAMEWORK_OWNED_SDK_FIELDS``. That makes the denylist fail-open when
``claude-agent-sdk`` adds a new field: the new field becomes valid as
``sdk_options`` automatically, even if it's a hooks-like lifecycle / security
knob that should have been denied.

This test plugs that gap: every dataclass field on ``ClaudeAgentOptions`` must
appear in exactly one of:

- ``_TYPED_MIRROR_SDK_FIELDS`` — coder_eval owns this via a top-level
  ``AgentConfig`` field; pass-through would silently shadow it.
- ``_FRAMEWORK_OWNED_SDK_FIELDS`` — transport / lifecycle / security-critical;
  denied for the YAML-visible knob.
- The user-visible allowlist (``_USER_VISIBLE_SDK_FIELDS``) — explicitly
  pass-through-able.

When a new SDK release lands, this test will fail until the field is
classified. The failure message names the unclassified fields so the
maintainer can decide where they belong.
"""

from __future__ import annotations

import dataclasses

from claude_agent_sdk import ClaudeAgentOptions

from coder_eval.models.agent_config import (
    _FRAMEWORK_OWNED_SDK_FIELDS,
    _USER_VISIBLE_SDK_FIELDS,
    _VALID_SDK_OPTION_FIELDS,
)


# Subset of ``_FRAMEWORK_OWNED_SDK_FIELDS`` mirrored as typed AgentConfig fields.
# Kept close to the model in spirit (see the inline comment groups in
# ``_FRAMEWORK_OWNED_SDK_FIELDS``); duplicated here so this test fails on its
# own if the typed-mirror set is reorganized without re-classifying.
_TYPED_MIRROR_SDK_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "permission_mode",
        "allowed_tools",
        "disallowed_tools",
        "plugins",
        "system_prompt",
        "system_prompt_file",
        "setting_sources",
        "settings",
    }
)


def test_valid_sdk_option_fields_matches_dataclass() -> None:
    """Sanity: the precomputed valid set must equal the actual SDK fields."""
    actual = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    assert actual == _VALID_SDK_OPTION_FIELDS


def test_every_sdk_field_is_classified() -> None:
    """Every SDK field must be either denied or in the user-visible allowlist.

    If this test fails after an SDK upgrade, decide for each new field:
    - **Pass-through-able**: nothing to do — it's automatically in
      ``_USER_VISIBLE_SDK_FIELDS`` (the leftover after the denylist).
    - **Framework-managed**: add it to ``_FRAMEWORK_OWNED_SDK_FIELDS`` in
      ``src/coder_eval/models/agent_config.py`` with a comment-group
      explanation.
    - **Typed-mirrored**: add a typed field to ``AgentConfig``, mirror its
      name in ``_FRAMEWORK_OWNED_SDK_FIELDS``, and update
      ``_TYPED_MIRROR_SDK_FIELDS`` above.

    Without this check, a future SDK field would silently slip through the
    pass-through validator (fail-open).
    """
    classified = _FRAMEWORK_OWNED_SDK_FIELDS | set(_USER_VISIBLE_SDK_FIELDS)
    unclassified = _VALID_SDK_OPTION_FIELDS - classified
    assert not unclassified, (
        f"Unclassified ClaudeAgentOptions field(s): {sorted(unclassified)}. "
        f"Classify each in src/coder_eval/models/agent_config.py — see this "
        f"test's docstring for guidance."
    )


def test_typed_mirror_subset_of_framework_owned() -> None:
    """Sanity: typed-mirrored fields must be in the denylist.

    Otherwise the pass-through validator would let YAML override a typed
    AgentConfig field (e.g. ``sdk_options.model`` would silently shadow
    ``agent.model``).
    """
    leak = _TYPED_MIRROR_SDK_FIELDS - _FRAMEWORK_OWNED_SDK_FIELDS
    assert not leak, f"Typed-mirror field(s) not in denylist: {sorted(leak)}"


def test_user_visible_and_framework_owned_are_disjoint() -> None:
    """The two sets must partition the SDK fields (no key on both lists)."""
    overlap = set(_USER_VISIBLE_SDK_FIELDS) & _FRAMEWORK_OWNED_SDK_FIELDS
    assert not overlap, f"Field(s) on both lists: {sorted(overlap)}"
