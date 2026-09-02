"""Shared runner used by tests/test_custom_lint.py.

Parses each file once, runs every registered rule, and filters violations
that are suppressed by an inline `# noqa: CE<id>` or bare `# noqa` comment.
"""

import ast
import re
from pathlib import Path

from tests.lint.rules.base import BaseRule
from tests.lint.rules.ce014_merge_strategy_declared import MergeStrategyDeclared
from tests.lint.rules.ce015_create_subprocess_limit import CreateSubprocessExplicitLimit
from tests.lint.rules.ce016_no_computed_tokenusage_kwargs import NoComputedTokenUsageKwargs
from tests.lint.rules.ce017_models_lazy_agent_imports import ModelsLazyAgentImports
from tests.lint.rules.ce018_no_final_status_name_denylist import NoFinalStatusNameDenylist
from tests.lint.rules.ce019_telemetry_non_fatal import TelemetryNonFatal
from tests.lint.rules.ce020_no_sdk_typed_base_agent_fields import NoSdkTypedBaseAgentFields
from tests.lint.rules.ce021_guarded_evaluationresult_parse import GuardedEvaluationResultParse
from tests.lint.rules.ce022_dialog_loop_statement_cap import SimulationDialogLoopStatementCap
from tests.lint.rules.ce023_no_proxy_shim_import import NoProxyShimImports
from tests.lint.rules.ce024_discriminated_unions import DiscriminatedUnions
from tests.lint.rules.ce032_criteria_path_seam import CriteriaPathSeam
from tests.lint.rules.ce037_no_dead_private_helper import NoDeadPrivateHelper
from tests.lint.rules.ce038_acquire_inside_try import AcquireInsideTry
from tests.lint.rules.ce039_config_error_escalates import ConfigErrorEscalates
from tests.lint.rules.ce043_no_command_output_truncation import NoCommandOutputTruncation
from tests.lint.rules.ce046_env_info_spreads_super import EnvInfoSpreadsSuper
from tests.lint.rules.ce047_embedded_shim_stdlib_only import EmbeddedShimStdlibOnly
from tests.lint.rules.no_agent_timing_access import NoAgentTimingAccess
from tests.lint.rules.no_blocking_io_in_async import NoBlockingIoInAsync
from tests.lint.rules.no_cli_imports_in_core import NoCliImportsInCore
from tests.lint.rules.no_silent_except import NoSilentExcept
from tests.lint.rules.no_submodule_model_imports import NoSubmoduleModelImports
from tests.lint.rules.no_top_level_run_limits_access import NoTopLevelRunLimitsAccess
from tests.lint.rules.no_transcript_regex_in_eval import NoTranscriptRegexInEval
from tests.lint.rules.no_type_name_string_dispatch import NoTypeNameStringDispatch
from tests.lint.rules.open_explicit_encoding import OpenExplicitEncoding
from tests.lint.rules.read_text_explicit_encoding import ReadTextExplicitEncoding
from tests.lint.rules.register_criterion_required import RegisterCriterionRequired
from tests.lint.rules.subprocess_run_explicit_encoding import SubprocessRunExplicitEncoding
from tests.lint.rules.yaml_models_forbid_extras import YamlModelsForbidExtras
from tests.lint.violation import Violation


type RuleClass = type[BaseRule]

ALL_RULES: list[RuleClass] = [
    NoSubmoduleModelImports,
    NoBlockingIoInAsync,
    RegisterCriterionRequired,
    NoCliImportsInCore,
    NoSilentExcept,
    NoAgentTimingAccess,
    NoTopLevelRunLimitsAccess,
    ReadTextExplicitEncoding,
    SubprocessRunExplicitEncoding,
    OpenExplicitEncoding,
    YamlModelsForbidExtras,
    NoTypeNameStringDispatch,
    NoTranscriptRegexInEval,
    MergeStrategyDeclared,
    CreateSubprocessExplicitLimit,
    NoComputedTokenUsageKwargs,
    ModelsLazyAgentImports,
    NoFinalStatusNameDenylist,
    TelemetryNonFatal,
    NoSdkTypedBaseAgentFields,
    GuardedEvaluationResultParse,
    SimulationDialogLoopStatementCap,
    NoProxyShimImports,
    DiscriminatedUnions,
    CriteriaPathSeam,
    NoDeadPrivateHelper,
    AcquireInsideTry,
    ConfigErrorEscalates,
    NoCommandOutputTruncation,
    EnvInfoSpreadsSuper,
    EmbeddedShimStdlibOnly,
]

# Anti-shadow invariant (mirrors AgentRegistry / register_pricing): every CE rule
# id must be unique. Suppression (a `noqa` comment with the rule id) and pytest parametrize ids key
# on the id string, so two rules sharing one id would silently alias each other
# (a single noqa would suppress both) — fail loudly at import time instead. A
# duplicate most plausibly arises when two in-flight branches claim the same next
# CE number; the loser must renumber.
_rule_ids = [r.id for r in ALL_RULES]
assert len(set(_rule_ids)) == len(_rule_ids), (
    f"duplicate CE rule id(s): {sorted({i for i in _rule_ids if _rule_ids.count(i) > 1})}"
)

_NOQA_ALL = re.compile(r"#\s*noqa\s*$")
_NOQA_CODES = re.compile(r"#\s*noqa:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)")


def _is_suppressed(source_lines: list[str], v: Violation) -> bool:
    """Honor `# noqa` placed on any line spanned by the offending AST node.

    AST nodes for multi-line statements report `lineno` at the start, so a
    `# noqa: CE002` placed on a closing paren or inner argument line was
    previously missed. We scan every physical line from `line` through
    `end_line` (inclusive) for a matching suppression marker.
    """
    if v.line == 0 or v.line > len(source_lines):
        return False
    end = max(v.line, v.end_line or v.line)
    end = min(end, len(source_lines))
    for i in range(v.line - 1, end):
        line = source_lines[i]
        m = _NOQA_CODES.search(line)
        if m and v.rule_id in {c.strip() for c in m.group(1).split(",")}:
            return True
        if not m and _NOQA_ALL.search(line):
            return True
    return False


def check_file(path: Path, rules: list[RuleClass] | None = None) -> list[Violation]:
    rules = rules if rules is not None else ALL_RULES
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [Violation(rule_id="CE000", file=str(path), line=e.lineno or 0, col=0, message=f"SyntaxError: {e.msg}")]
    except OSError as e:
        return [Violation(rule_id="CE000", file=str(path), line=0, col=0, message=f"IOError: {e}")]

    source_lines = source.splitlines()
    violations: list[Violation] = []
    for rule_class in rules:
        for v in rule_class(str(path)).check(tree):
            if not _is_suppressed(source_lines, v):
                violations.append(v)
    return violations


def check_paths(paths: list[Path], rules: list[RuleClass] | None = None) -> list[Violation]:
    violations: list[Violation] = []
    for path in paths:
        if path.is_dir():
            for py_file in sorted(path.rglob("*.py")):
                violations.extend(check_file(py_file, rules))
        elif path.suffix == ".py":
            violations.extend(check_file(path, rules))
    return sorted(violations, key=lambda v: (v.file, v.line, v.col))
