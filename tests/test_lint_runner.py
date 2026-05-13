"""Tests for the custom-lint runner — `# noqa` suppression and per-rule detection."""

from pathlib import Path

import pytest

from tests.lint.rules.no_blocking_io_in_async import NoBlockingIoInAsync
from tests.lint.rules.no_cli_imports_in_core import NoCliImportsInCore
from tests.lint.rules.no_silent_except import NoSilentExcept
from tests.lint.rules.no_submodule_model_imports import NoSubmoduleModelImports
from tests.lint.rules.register_criterion_required import RegisterCriterionRequired
from tests.lint.runner import check_file


@pytest.fixture
def write_py(tmp_path: Path):
    def _write(source: str, name: str = "sample.py") -> Path:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return path

    return _write


def test_noqa_on_violation_line_suppresses(write_py):
    """Single-line violation with `# noqa: CE002` on the same line is suppressed."""
    source = "import shutil\nasync def f(p):\n    shutil.rmtree(p)  # noqa: CE002 -- intentional\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoBlockingIoInAsync])
    assert violations == []


def test_noqa_on_closing_line_of_multiline_call_suppresses(write_py):
    """A `# noqa` on the closing line of a multi-line call should suppress."""
    source = (
        "import shutil\n"
        "async def f(p):\n"
        "    shutil.rmtree(\n"
        "        p,\n"
        "        ignore_errors=True,\n"
        "    )  # noqa: CE002 -- intentional\n"
    )
    path = write_py(source)
    violations = check_file(path, rules=[NoBlockingIoInAsync])
    assert violations == [], (
        f"Expected `# noqa: CE002` on the multi-line statement's closing line to suppress; got: {violations}"
    )


def test_noqa_on_inner_line_of_multiline_call_suppresses(write_py):
    """A `# noqa` on any line spanned by the AST node should suppress."""
    source = (
        "import shutil\n"
        "async def f(p):\n"
        "    shutil.rmtree(\n"
        "        p,  # noqa: CE002 -- intentional\n"
        "        ignore_errors=True,\n"
        "    )\n"
    )
    path = write_py(source)
    violations = check_file(path, rules=[NoBlockingIoInAsync])
    assert violations == [], f"Expected suppression on inner line; got: {violations}"


def test_violation_reported_when_no_noqa_anywhere(write_py):
    """Sanity: without any `# noqa`, the multi-line call is reported."""
    source = "import shutil\nasync def f(p):\n    shutil.rmtree(\n        p,\n    )\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoBlockingIoInAsync])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE002"


def test_bare_noqa_suppresses(write_py):
    """A bare `# noqa` (no code) should suppress any rule."""
    source = "def f():\n    try:\n        x = 1\n    except Exception:  # noqa\n        pass\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoSilentExcept])
    assert violations == []


# ---------- CE001 (NoSubmoduleModelImports) ----------


def test_ce001_flags_from_submodule_import(write_py):
    source = "from coder_eval.models.enums import AgentKind\n"
    path = write_py(source, name="caller.py")
    violations = check_file(path, rules=[NoSubmoduleModelImports])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE001"


def test_ce001_flags_bare_import_of_submodule(write_py):
    """A bare `import coder_eval.models.X` is also caught (visit_Import)."""
    source = "import coder_eval.models.criteria\n"
    path = write_py(source, name="caller.py")
    violations = check_file(path, rules=[NoSubmoduleModelImports])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE001"


def test_ce001_allows_top_level_import(write_py):
    source = "from coder_eval.models import AgentKind\n"
    path = write_py(source, name="caller.py")
    violations = check_file(path, rules=[NoSubmoduleModelImports])
    assert violations == []


def test_ce001_skips_files_inside_models(write_py, tmp_path):
    """Files inside coder_eval/models/ are allowed to do internal cross-imports."""
    sub = tmp_path / "coder_eval" / "models"
    sub.mkdir(parents=True)
    path = sub / "results.py"
    path.write_text("from coder_eval.models.enums import FinalStatus\n", encoding="utf-8")
    violations = check_file(path, rules=[NoSubmoduleModelImports])
    assert violations == []


def test_ce001_skips_type_checking_block(write_py):
    source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from coder_eval.models.enums import AgentKind\n"
    path = write_py(source, name="caller.py")
    violations = check_file(path, rules=[NoSubmoduleModelImports])
    assert violations == []


# ---------- CE003 (RegisterCriterionRequired) ----------


def test_ce003_flags_subclass_missing_decorator(write_py, tmp_path):
    sub = tmp_path / "coder_eval" / "criteria"
    sub.mkdir(parents=True)
    path = sub / "my_check.py"
    path.write_text("class MyCheck(BaseCriterion):\n    pass\n", encoding="utf-8")
    violations = check_file(path, rules=[RegisterCriterionRequired])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE003"


def test_ce003_passes_subclass_with_decorator(write_py, tmp_path):
    sub = tmp_path / "coder_eval" / "criteria"
    sub.mkdir(parents=True)
    path = sub / "my_check.py"
    path.write_text(
        "@register_criterion\nclass MyCheck(BaseCriterion):\n    pass\n",
        encoding="utf-8",
    )
    violations = check_file(path, rules=[RegisterCriterionRequired])
    assert violations == []


def test_ce003_skips_files_outside_criteria(write_py):
    source = "class MyCheck(BaseCriterion):\n    pass\n"
    path = write_py(source, name="elsewhere.py")
    violations = check_file(path, rules=[RegisterCriterionRequired])
    assert violations == []


# ---------- CE004 (NoCliImportsInCore) ----------


def test_ce004_flags_cli_import_in_core(write_py, tmp_path):
    sub = tmp_path / "coder_eval" / "evaluation"
    sub.mkdir(parents=True)
    path = sub / "thing.py"
    path.write_text("from coder_eval.cli.utils import something\n", encoding="utf-8")
    violations = check_file(path, rules=[NoCliImportsInCore])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE004"


def test_ce004_flags_bare_cli_import_in_core(write_py, tmp_path):
    sub = tmp_path / "coder_eval" / "criteria"
    sub.mkdir(parents=True)
    path = sub / "thing.py"
    path.write_text("import coder_eval.cli\n", encoding="utf-8")
    violations = check_file(path, rules=[NoCliImportsInCore])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE004"


def test_ce004_flags_in_models_layer(write_py, tmp_path):
    """models/ is part of the core layer in the expanded rule."""
    sub = tmp_path / "coder_eval" / "models"
    sub.mkdir(parents=True)
    path = sub / "thing.py"
    path.write_text("from coder_eval.cli import app\n", encoding="utf-8")
    violations = check_file(path, rules=[NoCliImportsInCore])
    assert len(violations) == 1


def test_ce004_allows_cli_import_in_cli(write_py, tmp_path):
    sub = tmp_path / "coder_eval" / "cli"
    sub.mkdir(parents=True)
    path = sub / "thing.py"
    path.write_text("from coder_eval.cli.utils import something\n", encoding="utf-8")
    violations = check_file(path, rules=[NoCliImportsInCore])
    assert violations == []


# ---------- CE005 tuple-form ----------


def test_ce005_flags_tuple_except_with_exception(write_py):
    """`except (Exception, OSError):` should be flagged when silently swallowed."""
    source = "def f():\n    try:\n        x = 1\n    except (Exception, OSError):\n        pass\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoSilentExcept])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE005"


def test_ce005_allows_tuple_except_without_exception(write_py):
    """A narrow tuple of specific exceptions is fine."""
    source = "def f():\n    try:\n        x = 1\n    except (OSError, ValueError):\n        pass\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoSilentExcept])
    assert violations == []


def test_ce005_bound_exception_referenced_passes(write_py):
    """`except Exception as e:` that uses `e` (e.g. `print(e)`) is not silent."""
    source = "def f():\n    try:\n        x = 1\n    except Exception as e:\n        print(e)\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoSilentExcept])
    assert violations == []


# ---------- CE006 agent timing access ----------


def test_ce006_flags_agent_max_turns_read(write_py):
    """`task.agent.max_turns` (read) should be flagged."""
    from tests.lint.rules.no_agent_timing_access import NoAgentTimingAccess

    source = "def f(task):\n    return task.agent.max_turns\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoAgentTimingAccess])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE006"


def test_ce006_flags_agent_turn_timeout_write(write_py):
    """`task.agent.turn_timeout = ...` (write) should be flagged."""
    from tests.lint.rules.no_agent_timing_access import NoAgentTimingAccess

    source = "def f(task):\n    task.agent.turn_timeout = 30\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoAgentTimingAccess])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE006"


def test_ce006_allows_top_level_max_turns(write_py):
    """`task.max_turns` (top-level field) should NOT be flagged."""
    from tests.lint.rules.no_agent_timing_access import NoAgentTimingAccess

    source = "def f(task):\n    return task.max_turns\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoAgentTimingAccess])
    assert violations == []


def test_ce006_allows_criterion_max_turns(write_py):
    """`criterion.max_turns` (real field on judge criteria) should NOT be flagged."""
    from tests.lint.rules.no_agent_timing_access import NoAgentTimingAccess

    source = "def f(criterion):\n    return criterion.max_turns\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoAgentTimingAccess])
    assert violations == []


# ---------- CE008 read_text explicit encoding ----------


def test_ce008_flags_read_text_without_encoding(write_py):
    from tests.lint.rules.read_text_explicit_encoding import ReadTextExplicitEncoding

    source = "from pathlib import Path\n\ndef f(p: Path):\n    return p.read_text()\n"
    path = write_py(source)
    violations = check_file(path, rules=[ReadTextExplicitEncoding])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE008"


def test_ce008_allows_read_text_with_encoding(write_py):
    from tests.lint.rules.read_text_explicit_encoding import ReadTextExplicitEncoding

    source = "from pathlib import Path\n\ndef f(p: Path):\n    return p.read_text(encoding='utf-8')\n"
    path = write_py(source)
    violations = check_file(path, rules=[ReadTextExplicitEncoding])
    assert violations == []


def test_ce008_flags_chained_read_text(write_py):
    """Chained call like ``(skill_dir / "SKILL.md").read_text()`` is also flagged."""
    from tests.lint.rules.read_text_explicit_encoding import ReadTextExplicitEncoding

    source = "from pathlib import Path\n\ndef f(p: Path):\n    return (p / 'a').read_text()\n"
    path = write_py(source)
    violations = check_file(path, rules=[ReadTextExplicitEncoding])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE008"


def test_ce008_noqa_suppresses(write_py):
    from tests.lint.rules.read_text_explicit_encoding import ReadTextExplicitEncoding

    source = "from pathlib import Path\n\ndef f(p: Path):\n    return p.read_text()  # noqa: CE008 -- intentional\n"
    path = write_py(source)
    violations = check_file(path, rules=[ReadTextExplicitEncoding])
    assert violations == []


# ---------- CE009 yaml models forbid extras ----------


def test_ce009_flags_basemodel_without_forbid(tmp_path: Path) -> None:
    """A BaseModel subclass in tasks.py without extra='forbid' is flagged."""
    from tests.lint.rules.yaml_models_forbid_extras import YamlModelsForbidExtras

    sub = tmp_path / "src" / "coder_eval" / "models"
    sub.mkdir(parents=True)
    path = sub / "tasks.py"
    path.write_text(
        "from pydantic import BaseModel\n\nclass Lax(BaseModel):\n    x: int\n",
        encoding="utf-8",
    )
    violations = check_file(path, rules=[YamlModelsForbidExtras])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE009"


def test_ce009_allows_basemodel_with_forbid(tmp_path: Path) -> None:
    from tests.lint.rules.yaml_models_forbid_extras import YamlModelsForbidExtras

    sub = tmp_path / "src" / "coder_eval" / "models"
    sub.mkdir(parents=True)
    path = sub / "tasks.py"
    path.write_text(
        "from pydantic import BaseModel, ConfigDict\n\nclass Strict(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n    x: int\n',
        encoding="utf-8",
    )
    violations = check_file(path, rules=[YamlModelsForbidExtras])
    assert violations == []


def test_ce009_allows_subclass_inheriting_forbid_from_same_file(tmp_path: Path) -> None:
    """Subclass of a same-file class that declares extra='forbid' inherits compliance."""
    from tests.lint.rules.yaml_models_forbid_extras import YamlModelsForbidExtras

    sub = tmp_path / "src" / "coder_eval" / "models"
    sub.mkdir(parents=True)
    path = sub / "criteria.py"
    path.write_text(
        "from pydantic import BaseModel, ConfigDict\n\nclass Base(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n\nclass Sub(Base):\n    x: int\n',
        encoding="utf-8",
    )
    violations = check_file(path, rules=[YamlModelsForbidExtras])
    assert violations == []


def test_ce008_skips_files_outside_scope(tmp_path: Path) -> None:
    """Files outside tasks.py / criteria.py are not flagged (results.py uses extra='allow')."""
    from tests.lint.rules.yaml_models_forbid_extras import YamlModelsForbidExtras

    sub = tmp_path / "src" / "coder_eval" / "models"
    sub.mkdir(parents=True)
    path = sub / "results.py"
    path.write_text(
        "from pydantic import BaseModel\n\nclass Lax(BaseModel):\n    x: int\n",
        encoding="utf-8",
    )
    violations = check_file(path, rules=[YamlModelsForbidExtras])
    assert violations == []
