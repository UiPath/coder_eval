"""Cross-platform test helpers for PATH-resolution tests."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def tmp_subdir(*parts: str) -> Path:
    """Return a cross-platform temp-style ``Path`` for use in tests.

    Use this when a test needs a ``Path`` value as an opaque carrier (e.g.
    a ``BatchRunConfig(run_dir=...)`` exercising validator logic) but never
    actually touches the filesystem at that path. ``Path("/tmp/x")`` parses
    on Windows but obscures intent; ``tmp_subdir("x")`` produces
    ``C:\\Users\\…\\AppData\\Local\\Temp\\x`` on Windows and ``/tmp/x`` on
    POSIX while reading the same way at the call site.

    For tests that DO create files on disk, use pytest's ``tmp_path``
    fixture instead — it cleans up automatically per-test.
    """
    return Path(tempfile.gettempdir(), *parts)


# ``label`` is interpolated raw into the shim body. Restricting to a short
# safe alphabet (letters, digits, ``-``/``_``) closes the foot-gun where a
# future caller could accidentally pass shell-active characters and have
# them execute at test time. Current callers only need ``"stale"`` /
# ``"agent"`` / similar identifiers, so the restriction costs nothing.
_SAFE_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def write_uip_shim(directory: Path, label: str) -> Path:
    """Write a platform-appropriate ``uip`` shim that echoes ``label``.

    POSIX: ``<dir>/uip`` shell script with ``chmod 0o755``.
    Windows: ``<dir>/uip.cmd`` batch file — bare ``uip`` is not in ``PATHEXT``
    and ``shell=True`` invocation of ``uip`` would otherwise fail with
    ``'uip' is not recognized as an internal or external command``.

    Tests that probe PATH-resolution behavior should use this so they
    pass on both runners without OS-specific scaffolding.

    Raises:
        ValueError: If ``label`` contains characters outside ``[A-Za-z0-9_-]``.
    """
    if not _SAFE_LABEL_PATTERN.match(label):
        raise ValueError(f"write_uip_shim label must match {_SAFE_LABEL_PATTERN.pattern!r}; got {label!r}")
    if os.name == "nt":
        shim = directory / "uip.cmd"
        shim.write_text(f"@echo {label}\r\n", encoding="utf-8")
    else:
        shim = directory / "uip"
        shim.write_text(f"#!/bin/sh\necho {label}\n", encoding="utf-8")
        shim.chmod(0o755)
    return shim
