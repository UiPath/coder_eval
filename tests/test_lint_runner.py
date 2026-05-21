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


# ---------- CE010 subprocess.run explicit encoding ----------


def test_ce010_flags_subprocess_run_text_without_encoding(write_py):
    from tests.lint.rules.subprocess_run_explicit_encoding import SubprocessRunExplicitEncoding

    source = "import subprocess\n\ndef f():\n    subprocess.run(['ls'], text=True, capture_output=True)\n"
    path = write_py(source)
    violations = check_file(path, rules=[SubprocessRunExplicitEncoding])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE010"


def test_ce010_allows_subprocess_run_text_with_encoding(write_py):
    from tests.lint.rules.subprocess_run_explicit_encoding import SubprocessRunExplicitEncoding

    source = "import subprocess\n\ndef f():\n    subprocess.run(['ls'], text=True, encoding='utf-8')\n"
    path = write_py(source)
    violations = check_file(path, rules=[SubprocessRunExplicitEncoding])
    assert violations == []


def test_ce010_ignores_subprocess_run_byte_mode(write_py):
    """Byte-mode subprocess.run (no text=True) doesn't need encoding."""
    from tests.lint.rules.subprocess_run_explicit_encoding import SubprocessRunExplicitEncoding

    source = "import subprocess\n\ndef f():\n    subprocess.run(['ls'], capture_output=True)\n"
    path = write_py(source)
    violations = check_file(path, rules=[SubprocessRunExplicitEncoding])
    assert violations == []


def test_ce010_flags_universal_newlines_alias(write_py):
    """The legacy ``universal_newlines=True`` alias must also be flagged."""
    from tests.lint.rules.subprocess_run_explicit_encoding import SubprocessRunExplicitEncoding

    source = "import subprocess\n\ndef f():\n    subprocess.run(['ls'], universal_newlines=True)\n"
    path = write_py(source)
    violations = check_file(path, rules=[SubprocessRunExplicitEncoding])
    assert len(violations) == 1


def test_ce010_covers_check_output_and_friends(write_py):
    """``check_output`` / ``call`` / ``check_call`` / ``Popen`` accept the same kwargs."""
    from tests.lint.rules.subprocess_run_explicit_encoding import SubprocessRunExplicitEncoding

    source = (
        "import subprocess\n"
        "\n"
        "def f():\n"
        "    subprocess.check_output(['ls'], text=True)\n"
        "    subprocess.call(['ls'], text=True)\n"
        "    subprocess.check_call(['ls'], text=True)\n"
        "    subprocess.Popen(['ls'], text=True)\n"
    )
    path = write_py(source)
    violations = check_file(path, rules=[SubprocessRunExplicitEncoding])
    assert len(violations) == 4


# ---------- CE011 open() explicit encoding ----------


def test_ce011_flags_open_without_encoding(write_py):
    from tests.lint.rules.open_explicit_encoding import OpenExplicitEncoding

    source = "def f(p):\n    with open(p) as fh:\n        return fh.read()\n"
    path = write_py(source)
    violations = check_file(path, rules=[OpenExplicitEncoding])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE011"


def test_ce011_allows_open_with_encoding(write_py):
    from tests.lint.rules.open_explicit_encoding import OpenExplicitEncoding

    source = "def f(p):\n    with open(p, encoding='utf-8') as fh:\n        return fh.read()\n"
    path = write_py(source)
    violations = check_file(path, rules=[OpenExplicitEncoding])
    assert violations == []


def test_ce011_ignores_byte_mode(write_py):
    """``open(p, 'rb')`` returns bytes — no encoding kwarg needed."""
    from tests.lint.rules.open_explicit_encoding import OpenExplicitEncoding

    source = "def f(p):\n    with open(p, 'rb') as fh:\n        return fh.read()\n"
    path = write_py(source)
    violations = check_file(path, rules=[OpenExplicitEncoding])
    assert violations == []


def test_ce011_ignores_write_byte_mode(write_py):
    """``open(p, 'wb')`` writes bytes — no encoding kwarg needed."""
    from tests.lint.rules.open_explicit_encoding import OpenExplicitEncoding

    source = "def f(p, data):\n    with open(p, 'wb') as fh:\n        fh.write(data)\n"
    path = write_py(source)
    violations = check_file(path, rules=[OpenExplicitEncoding])
    assert violations == []


def test_ce011_flags_explicit_text_mode_without_encoding(write_py):
    """``open(p, 'w')`` is text mode — must pass encoding."""
    from tests.lint.rules.open_explicit_encoding import OpenExplicitEncoding

    source = "def f(p, data):\n    with open(p, 'w') as fh:\n        fh.write(data)\n"
    path = write_py(source)
    violations = check_file(path, rules=[OpenExplicitEncoding])
    assert len(violations) == 1


def test_ce011_ignores_path_open_method(write_py):
    """``Path.open(...)`` is a method call, not the builtin — out of scope."""
    from tests.lint.rules.open_explicit_encoding import OpenExplicitEncoding

    source = "from pathlib import Path\n\ndef f(p: Path):\n    with p.open() as fh:\n        return fh.read()\n"
    path = write_py(source)
    violations = check_file(path, rules=[OpenExplicitEncoding])
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


# ---------- CE012 no type-name string dispatch ----------


def test_ce012_flags_eq_against_string_literal(write_py):
    """``type(x).__name__ == "Foo"`` is subclass-blind — flag it."""
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = 'def f(msg):\n    if type(msg).__name__ == "SystemMessage":\n        return None\n'
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE012"


def test_ce012_flags_ne_against_string_literal(write_py):
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = 'def f(msg):\n    return type(msg).__name__ != "AssistantMessage"\n'
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert len(violations) == 1


def test_ce012_flags_reversed_comparison(write_py):
    """The literal-first form ``"Foo" == type(x).__name__`` is also flagged."""
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = 'def f(msg):\n    return "ResultMessage" == type(msg).__name__\n'
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert len(violations) == 1


def test_ce012_flags_in_tuple_of_string_literals(write_py):
    """Membership against a tuple of name literals is the same anti-pattern."""
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = 'def f(msg):\n    return type(msg).__name__ in ("SystemMessage", "UserMessage")\n'
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert len(violations) == 1


def test_ce012_allows_isinstance(write_py):
    """``isinstance`` is the recommended form — must not be flagged."""
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = "def f(msg):\n    return isinstance(msg, SystemMessage)\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert violations == []


def test_ce012_allows_type_name_in_format_string(write_py):
    """``f'got {type(x).__name__}'`` is a diagnostic, not a dispatch — must not flag."""
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = 'def f(msg):\n    return f"got {type(msg).__name__}"\n'
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert violations == []


def test_ce012_allows_type_name_compared_to_variable(write_py):
    """Comparing against a variable (not a string literal) is data-driven — allow it."""
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = "def f(msg, expected):\n    return type(msg).__name__ == expected\n"
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert violations == []


def test_ce012_noqa_suppresses(write_py):
    from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch

    source = 'def f(msg):\n    return type(msg).__name__ == "SystemMessage"  # noqa: CE012 -- intentional\n'
    path = write_py(source)
    violations = check_file(path, rules=[NoTypeNameStringDispatch])
    assert violations == []


# --- CE013 ---


@pytest.fixture
def write_eval_py(tmp_path: Path):
    """Write a .py file under a simulated ``src/coder_eval/evaluation/`` path
    so CE013's scope check (``"/coder_eval/evaluation/" in filepath``) fires."""

    def _write(source: str, name: str = "sample.py") -> Path:
        target_dir = tmp_path / "src" / "coder_eval" / "evaluation"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        path.write_text(source, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def write_criteria_py(tmp_path: Path):
    def _write(source: str, name: str = "sample.py") -> Path:
        target_dir = tmp_path / "src" / "coder_eval" / "criteria"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        path.write_text(source, encoding="utf-8")
        return path

    return _write


def test_ce013_flags_re_compile_with_tag_pattern_in_evaluation(write_eval_py):
    from tests.lint.rules.no_transcript_regex_in_eval import NoTranscriptRegexInEval

    source = 'import re\n_RE = re.compile(r"(?:^|\\n)\\[RESULT - [A-Z_]+\\] ")\n'
    path = write_eval_py(source)
    violations = check_file(path, rules=[NoTranscriptRegexInEval])
    assert len(violations) == 1
    assert violations[0].rule_id == "CE013"


def test_ce013_flags_re_search_with_json_shape_in_criteria(write_criteria_py):
    from tests.lint.rules.no_transcript_regex_in_eval import NoTranscriptRegexInEval

    # The string literal passed to re.search must contain the verbatim sequence ``"score"``
    # so the rule's marker check fires. Use a Python triple-quoted source so the inner
    # quotes survive into the literal at the AST level.
    source = "import re\ndef f(s):\n    return re.search('\"score\":', s)\n"
    path = write_criteria_py(source)
    violations = check_file(path, rules=[NoTranscriptRegexInEval])
    assert len(violations) == 1


def test_ce013_allows_re_in_other_packages(write_py):
    """``re.compile`` in a non-evaluation/criteria module is fine (e.g. orchestrator)."""
    from tests.lint.rules.no_transcript_regex_in_eval import NoTranscriptRegexInEval

    source = 'import re\n_RE = re.compile(r"(?:^|\\n)\\[ASSISTANT\\] ")\n'
    path = write_py(source)
    violations = check_file(path, rules=[NoTranscriptRegexInEval])
    assert violations == []


def test_ce013_allows_noqa_suppression(write_eval_py):
    from tests.lint.rules.no_transcript_regex_in_eval import NoTranscriptRegexInEval

    source = 'import re\n_RE = re.compile(r"\\[RESULT - X\\] ")  # noqa: CE013 -- legacy\n'
    path = write_eval_py(source)
    violations = check_file(path, rules=[NoTranscriptRegexInEval])
    assert violations == []


def test_ce013_does_not_flag_user_regex_compile_in_file_matches_regex(write_criteria_py):
    """A user-supplied non-transcript pattern (no ``[ASSISTANT]`` / ``[RESULT`` / score literal)
    must not trip the rule. ``criteria/file_matches_regex.py`` compiles arbitrary user patterns."""
    from tests.lint.rules.no_transcript_regex_in_eval import NoTranscriptRegexInEval

    source = "import re\ndef f(p):\n    return re.compile(p)\n"  # user-supplied pattern, no literal
    path = write_criteria_py(source)
    violations = check_file(path, rules=[NoTranscriptRegexInEval])
    assert violations == []


def test_ce013_would_have_caught_parse_judge_verdict_regex(write_eval_py):
    """Regression test: pin the historical defect class so a future change can't silently
    weaken the rule. The exact line from the legacy parser must trigger CE013."""
    from tests.lint.rules.no_transcript_regex_in_eval import NoTranscriptRegexInEval

    source = 'import re\n_RESULT_TAG_RE = re.compile(r"(?:^|\\n)\\[RESULT - [A-Z_]+\\] ")\n'
    path = write_eval_py(source)
    violations = check_file(path, rules=[NoTranscriptRegexInEval])
    assert len(violations) == 1, "rule must catch the historical [RESULT - …] pattern"
