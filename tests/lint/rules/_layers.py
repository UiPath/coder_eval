"""The core-layer predicate, declared once and shared by CE004 and CE066.

Both rules ask the same question — "is this file in the core layer?" — and a
second copy of the answer is how a package added to one regex silently escapes
the other. ``_model_ctor.py`` is the in-tree precedent for a ``_``-prefixed
shared rule helper.

The boundary is stated as an ALLOWLIST of what is *not* core, because the
denylist form is what leaves holes: an earlier draft named only
``orchestrator.py`` as the top-level core module, which exempted
``result_metrics.py`` — the very module CE066's fix message tells a violator to
move their metric into — along with ``run_record.py``, ``stats.py`` and
``timing.py``. Every ``.py`` directly under ``src/coder_eval/`` is core; the
non-core layers are the ``cli/`` and ``reports/`` packages, which are named.
"""

import re


_CORE_DIRS = re.compile(
    r"[/\\](criteria|evaluation|models|simulation|scoring|streaming|errors|orchestration|agents|harbor)[/\\]"
)
# Every module directly under src/coder_eval/ — orchestrator.py, result_metrics.py,
# run_record.py, timing.py and friends. No directory regex can see these.
_TOP_LEVEL_CORE = re.compile(r"[/\\]coder_eval[/\\][A-Za-z_][A-Za-z0-9_]*\.py$")


def is_core_path(filepath: str) -> bool:
    """Whether ``filepath`` belongs to the core layer."""
    return bool(_CORE_DIRS.search(filepath) or _TOP_LEVEL_CORE.search(filepath))
