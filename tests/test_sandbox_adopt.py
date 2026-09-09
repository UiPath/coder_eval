"""`Sandbox.adopt` — grade a workspace in place instead of copying it.

The behavior that matters is what adopt does NOT do. `setup()` materializes a
workspace (copies templates in, writes shims, builds a venv); `adopt()` takes one
that already exists and only derives the environment around it. A regression that
made adopt materialize anything would overwrite the very files being graded, and
would do so silently — the criteria would still run, just against different
content. So each assertion below pins one thing staying untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_eval.models import SandboxConfig
from coder_eval.sandbox import Sandbox


def _workspace(tmp_path: Path) -> Path:
    """A workspace shaped like an agent's output: real files plus build output."""
    ws = tmp_path / "ws"
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "dist").mkdir()
    (ws / "src.py").write_text("print('hi')", encoding="utf-8")
    (ws / "node_modules" / "pkg" / "index.js").write_text("module.exports=1", encoding="utf-8")
    (ws / "dist" / "bundle.js").write_text("bundled", encoding="utf-8")
    return ws


def _sandbox(**cfg: object) -> Sandbox:
    return Sandbox(SandboxConfig(**cfg), task_id="t")  # type: ignore[arg-type]


def test_adopt_uses_the_directory_itself(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    sandbox = _sandbox()
    assert sandbox.adopt(ws) == ws.resolve()
    assert sandbox.sandbox_dir == ws.resolve(), "adopt must not create a copy"


def test_adopt_exposes_files_the_copy_path_filters_out(tmp_path: Path) -> None:
    """The bug this fixes. `_should_ignore_template_file` drops node_modules,
    dist, build and .venv, so on the copy path a criterion like
    `test -f dist/bundle.js` fails as a COPYING artifact rather than as a
    verdict on the agent's work."""
    ws = _workspace(tmp_path)
    sandbox = _sandbox()
    sandbox.adopt(ws)
    assert sandbox.sandbox_dir is not None
    assert (sandbox.sandbox_dir / "dist" / "bundle.js").is_file()
    assert (sandbox.sandbox_dir / "node_modules" / "pkg" / "index.js").is_file()


def test_adopt_writes_nothing_into_the_workspace(tmp_path: Path) -> None:
    """No shims, no venv, no template files — the graded tree is exactly as found."""
    ws = _workspace(tmp_path)
    before = {p.relative_to(ws) for p in ws.rglob("*")}
    _sandbox().adopt(ws)
    assert {p.relative_to(ws) for p in ws.rglob("*")} == before


def test_adopt_never_owns_the_directory(tmp_path: Path) -> None:
    """cleanup() must not delete a directory the caller handed us."""
    ws = _workspace(tmp_path)
    sandbox = _sandbox()
    sandbox.adopt(ws)
    assert sandbox.is_persistent
    sandbox.cleanup()
    assert ws.is_dir(), "cleanup deleted an adopted workspace"
    assert (ws / "src.py").is_file()


def test_adopt_discovers_an_existing_venv_without_creating_one(tmp_path: Path) -> None:
    """The execute phase already built the venv; re-creating it would mutate the
    graded tree. Discovery keeps `run_command` criteria on the agent's PATH."""
    ws = _workspace(tmp_path)
    (ws / ".venv" / "bin").mkdir(parents=True)
    sandbox = _sandbox(python={"env_packages": []})
    sandbox.adopt(ws)
    assert sandbox.venv_dir == ws.resolve() / ".venv"


def test_adopt_leaves_venv_unset_when_there_is_none(tmp_path: Path) -> None:
    sandbox = _sandbox(python={"env_packages": []})
    sandbox.adopt(_workspace(tmp_path))
    assert sandbox.venv_dir is None


def test_adopt_rejects_the_docker_driver(tmp_path: Path) -> None:
    """A container workspace is not reachable from the host, so adopting one
    would silently grade whatever happens to sit at that host path."""
    sandbox = _sandbox(driver="docker")
    with pytest.raises(RuntimeError, match="host-side only"):
        sandbox.adopt(_workspace(tmp_path))


def test_adopt_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not an existing directory"):
        _sandbox().adopt(tmp_path / "nope")


def test_adopt_rejects_a_file(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not an existing directory"):
        _sandbox().adopt(f)
