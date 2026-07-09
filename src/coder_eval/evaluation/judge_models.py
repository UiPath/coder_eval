"""Model-name translations between vendor-prefixed ids and the run's backend for judge-style criteria.

Pure, side-effect-free string utilities. Bedrock helper delegates to the
existing ``to_bedrock_inference_profile`` so cross-region prefix logic stays
in one place.
"""

from __future__ import annotations

import re

from coder_eval.models import to_bedrock_inference_profile


_VERSION_SUFFIX_RE = re.compile(r"-v\d+(?::\d+)?$")


def _strip_version_suffix(model: str) -> str:
    return _VERSION_SUFFIX_RE.sub("", model)


def to_bedrock_model(model: str, region: str) -> str:
    """Vendor-prefixed model id -> Bedrock cross-region inference-profile id.

    Strips ``-vN`` / ``-vN:M`` suffix, then applies vendor + region prefix
    via ``to_bedrock_inference_profile``. Idempotent on already-qualified ids.

    Raises:
        ValueError: ``model`` empty after stripping.
    """
    stripped = _strip_version_suffix(model.strip())
    qualified = to_bedrock_inference_profile(stripped, region)
    if not qualified:
        raise ValueError(f"Cannot translate empty model to Bedrock id (input={model!r})")
    return qualified


def to_anthropic_alias(model: str) -> str:
    """Vendor-prefixed model id -> bare Anthropic alias.

    Strips leading ``anthropic.`` vendor prefix and trailing ``-vN[:M]`` suffix.
    Idempotent on a bare alias.

    Raises:
        ValueError: ``model`` empty after stripping.
    """
    stripped = model.strip()
    if stripped.startswith("anthropic."):
        stripped = stripped[len("anthropic.") :]
    stripped = _strip_version_suffix(stripped)
    if not stripped:
        raise ValueError(f"Cannot translate empty model to Anthropic alias (input={model!r})")
    return stripped
