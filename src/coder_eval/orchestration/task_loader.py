"""Task definition loading and validation."""

from __future__ import annotations

import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from ..models import (
    ROW_SELECTOR_FLAGS,
    AgentConfig,
    BaseAgentConfig,
    Dataset,
    ExperimentVariant,
    TaskDefinition,
    TemplateDirSource,
    TemplateSource,
    copy_with,
)


# Fixed seed for the CLI --sample (max_rows) uniform draw: a smoke sample should
# be reproducible run-to-run, just not first-path-biased like a raw slice.
_SMOKE_SAMPLE_SEED = 0

_ROW_VAR_PATTERN = re.compile(r"\$\{row\.([A-Za-z_][A-Za-z0-9_]*)\}")
_ROW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

logger = logging.getLogger(__name__)


class SplitSelectorError(ValueError):
    """A CLI ``--split`` selector eliminated every row of a labelled dataset.

    Distinct from the other dataset errors on purpose. Those describe a malformed
    FILE and are demoted to ``skipped_tasks`` so one bad task cannot abort a suite;
    this one describes a malformed INVOCATION — the user asked for a split that does
    not exist — and there is no per-task isolation argument for it, because the same
    selector is applied to every task in the run. Demoted, it produces a yellow
    warning and exit 0: a CI gate that ran zero evals and reported success.

    That the abort is run-wide is the point, not a side effect: ``--split`` is global
    to the invocation, so one labelled suite with no matching row means the selector
    itself is wrong, and finishing the other suites would hide it.

    A ``ValueError`` subclass so every existing ``except ValueError`` caller keeps
    working; ``resolve_all_tasks`` re-raises it explicitly and the CLI turns it into
    a ``typer.BadParameter``.
    """


def load_task(task_file: Path) -> tuple[TaskDefinition, str]:
    """Load a task definition from a YAML file.

    Args:
        task_file: Path to the task YAML file

    Returns:
        Tuple of (parsed TaskDefinition, raw YAML text)

    Raises:
        FileNotFoundError: If task file doesn't exist
        ValueError: If task file is invalid
    """
    if not task_file.exists():
        raise FileNotFoundError(f"Task file not found: {task_file}")

    if task_file.is_dir():
        msg = (
            f"Expected a YAML task file but got a directory: {task_file}\n"
            f"Hint: use a glob pattern like '{task_file}/*.yaml' to select task files."
        )
        raise ValueError(msg)

    raw_yaml = task_file.read_text(encoding="utf-8")
    task_data = yaml.safe_load(raw_yaml)

    try:
        # CE041 exemption: `task_data` is `yaml.safe_load` output. `TaskDefinition` is the ONE
        # target here that does NOT declare extra="forbid" (the top-level schema is deliberately
        # in soft launch), so an unknown key is dropped rather than raised — but not silently:
        # `_warn_on_unknown_fields` emits an UnknownTaskFieldWarning that `coder-eval plan`
        # renders inline. Converting to model_validate must preserve the
        # "Invalid task definition: ..." message the handler below produces; recorded as a
        # follow-up in .claude/harness-candidates.md.
        task = TaskDefinition(**task_data)  # noqa: CE041
        # Resolve relative template paths
        task = resolve_template_paths(task, task_file.parent)
        task = resolve_initial_prompt_file(task, task_file.parent)
        task = resolve_system_prompt_files(task, task_file.parent)
        task = resolve_dockerfile_path(task, task_file.parent)
        return task, raw_yaml
    except Exception as e:
        raise ValueError(f"Invalid task definition: {e}") from e


def resolve_template_source_paths(sources: list[TemplateSource], base_dir: Path) -> None:
    """Resolve TemplateDirSource paths to absolute, in place.

    Expands $VAR / ${VAR} environment variables, then normalizes the path:
    relative paths are resolved against ``base_dir``; absolute paths are
    used as-is (but still go through ``Path(...)`` for string normalization).

    Undefined env variables raise ``ValueError`` — a template directory is a
    load-bearing config field and an unresolved variable would otherwise
    surface as a cryptic "Template directory not found" error at sandbox
    setup, far from the actual configuration mistake.

    Scope: only environment variables (``$VAR`` / ``${VAR}``) are expanded
    here. Dataset row substitution (``${row.field}`` in ``expand_dataset``)
    runs over ``initial_prompt`` and ``success_criteria`` only — it does
    NOT touch ``sandbox.template_sources``. The two regexes are disjoint
    (env requires ``[A-Za-z_][A-Za-z0-9_]*``, row-var requires the dot)
    but a ``${row.X}`` left inside a template path will not be substituted
    and will fail at sandbox setup.

    Skips non-TemplateDirSource entries.

    Args:
        sources: List of template sources (TemplateDirSource, RepoSource, etc.)
        base_dir: Base directory for resolving relative paths.

    Raises:
        ValueError: If a ``TemplateDirSource.path`` references an undefined
            environment variable.
    """
    for source in sources:
        if isinstance(source, TemplateDirSource):
            raw = source.path
            undefined: list[str] = []
            for match in _ENV_VAR_PATTERN.finditer(raw):
                var_name = match.group(1) or match.group(2)
                if var_name not in os.environ:
                    undefined.append(var_name)
            if undefined:
                names = ", ".join(f"${v}" for v in undefined)
                msg = (
                    f"Template path {raw!r} references undefined environment variable(s): {names}. "
                    f"Set them before loading the task (e.g. in .env) so the template directory can be resolved."
                )
                raise ValueError(msg)
            expanded = os.path.expandvars(raw)
            template_path = Path(expanded)
            if template_path.is_absolute():
                source.path = str(template_path)
            else:
                source.path = str((base_dir / template_path).resolve())


def resolve_template_paths(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve relative template paths to absolute paths.

    Mutates TemplateDirSource.path in place. Other source types don't need resolution.

    Args:
        task: Task definition with possibly relative paths
        base_dir: Directory containing the task YAML file

    Returns:
        Task with resolved absolute paths (modified in place)
    """
    if task.sandbox.template_sources:
        resolve_template_source_paths(task.sandbox.template_sources, base_dir)

    return task


def resolve_dockerfile_path(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve ``sandbox.docker.dockerfile_path`` to an absolute path, in place.

    When set, ``dockerfile_path`` is interpreted relative to the task YAML's
    directory (``base_dir``), with ``$VAR`` / ``${VAR}`` environment variables
    expanded first (mirroring :func:`resolve_template_source_paths`). The
    resolved file must exist -- a missing Dockerfile is a configuration error
    surfaced at load time rather than as an opaque ``docker build`` failure.

    No-op when ``dockerfile_path`` is unset. Resolution runs regardless of the
    configured ``driver`` so the absolute path stays stable even if a later
    layer flips the driver to ``docker``.

    Args:
        task: Task definition possibly carrying a relative ``dockerfile_path``.
        base_dir: Directory containing the task YAML file.

    Returns:
        The same task with an absolute ``dockerfile_path`` (modified in place).

    Raises:
        FileNotFoundError: If the resolved Dockerfile does not exist.
    """
    docker_cfg = task.sandbox.docker
    raw = docker_cfg.dockerfile_path
    if raw is None:
        return task
    dockerfile = Path(os.path.expandvars(raw))
    if not dockerfile.is_absolute():
        dockerfile = (base_dir / dockerfile).resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")
    docker_cfg.dockerfile_path = str(dockerfile)
    return task


def resolve_initial_prompt_file(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve initial_prompt_file to inline initial_prompt.

    In simulation mode, both ``initial_prompt`` and ``initial_prompt_file`` may
    be absent — the simulator generates the opening user utterance itself.
    """
    if task.initial_prompt_file is not None:
        prompt_path = Path(task.initial_prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = (base_dir / prompt_path).resolve()
        if not prompt_path.exists():
            raise FileNotFoundError(f"initial_prompt_file not found: {prompt_path}")
        content = prompt_path.read_text(encoding="utf-8").strip()
        # Clear file field BEFORE setting inline to avoid mutual-exclusivity validator
        task.initial_prompt_file = None
        task.initial_prompt = content
    if task.initial_prompt is None:
        in_simulation = task.simulation is not None and task.simulation.enabled
        if not in_simulation and not task.is_none_agent:
            raise ValueError(
                "Either 'initial_prompt' or 'initial_prompt_file' must be set "
                + "(unless 'simulation.enabled' is true, in which case the simulator generates the opener, "
                + "or 'agent.type' is 'none', in which case no agent runs)"
            )
    return task


def resolve_variant_initial_prompt_file(variant: ExperimentVariant, base_dir: Path) -> None:
    """Resolve initial_prompt_file on a variant to inline initial_prompt. Mutates in place.

    Args:
        variant: The experiment variant (may have initial_prompt_file set).
        base_dir: Directory to resolve relative paths against (experiment YAML dir).

    Raises:
        FileNotFoundError: If the file doesn't exist.
    """
    if variant.initial_prompt_file is None:
        return
    prompt_path = Path(variant.initial_prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = (base_dir / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(f"variant initial_prompt_file not found: {prompt_path}")
    content = prompt_path.read_text(encoding="utf-8").strip()
    # Clear file field BEFORE setting inline to avoid mutual-exclusivity validator
    variant.initial_prompt_file = None
    variant.initial_prompt = content


def resolve_agent_system_prompt[T: AgentConfig | BaseAgentConfig | None](agent_config: T, base_dir: Path) -> T:
    """Inline ``system_prompt_file`` into ``system_prompt``, returning the resolved config.

    Returns a NEW config rather than mutating in place because the swap has no
    valid sequential order: ``BaseAgentConfig`` sets ``validate_assignment=True``,
    and the two prompt fields are constrained against each other in both
    directions — clearing the file first leaves ``(mode='replace', prompt=None,
    file=None)``, which ``check_replace_mode_has_prompt`` rejects, while setting
    the prompt first leaves both populated, which ``check_prompt_exclusivity``
    rejects. A single ``model_copy(update=...)`` applies both edits at once so no
    half-updated state is ever validated.

    Args:
        agent_config: Config to resolve; ``None`` and configs without a
            ``system_prompt_file`` are returned unchanged.
        base_dir: Directory that relative ``system_prompt_file`` paths resolve against.

    Returns:
        The resolved config — the same object when there was nothing to inline.

    Raises:
        FileNotFoundError: If ``system_prompt_file`` does not exist.
        ValueError: If the file is blank under ``system_prompt_mode: replace``.
    """
    if agent_config is None or agent_config.system_prompt_file is None:
        return agent_config
    prompt_path = Path(agent_config.system_prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = (base_dir / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(f"system_prompt_file not found: {prompt_path}")
    # A whitespace-only file is no prompt at all — mirror the normalization
    # _blank_prompt_is_no_prompt applies to inline prompts (copy_with delegates to
    # model_copy, which skips validators, so this seam has to apply it itself).
    content = prompt_path.read_text(encoding="utf-8").strip() or None
    # ...which means the copy also skips check_replace_mode_has_prompt, so a
    # blank file under `replace` would reach the agent as (replace, no prompt)
    # and silently downgrade to the append preset at runtime. Reject it here
    # instead: the file is the only prompt the config had, and the docs promise
    # this combination fails at load.
    if content is None and getattr(agent_config, "system_prompt_mode", "append") == "replace":
        raise ValueError(
            f"system_prompt_file {prompt_path} is empty; system_prompt_mode='replace' requires a "
            + "prompt to replace the Claude Code default with"
        )
    return copy_with(agent_config, system_prompt=content, system_prompt_file=None)


def resolve_system_prompt_files(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve system_prompt_file on agent config."""
    task.agent = resolve_agent_system_prompt(task.agent, base_dir)
    return task


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts."""
    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Dataset {path}: invalid JSON on line {line_num}: {e}") from e
            if not isinstance(row, dict):
                raise ValueError(f"Dataset {path}: row on line {line_num} is not a JSON object: {row!r}")
            rows.append(row)
    return rows


def _resolve_path(p: str, task_file_dir: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (task_file_dir / path).resolve()


def load_dataset_rows(dataset: Dataset, task_file_dir: Path) -> list[dict[str, Any]]:
    """Load dataset rows from inline list or one or more JSONL files."""
    if dataset.rows is not None:
        return [dict(r) for r in dataset.rows]

    assert dataset.paths is not None  # guaranteed by Dataset.check_source
    rows: list[dict[str, Any]] = []
    for p in dataset.paths:
        rows.extend(_load_jsonl(_resolve_path(p, task_file_dir)))
    return rows


def stratum_key(row: dict[str, Any], field: str) -> str:
    """The stratum a row belongs to under ``_stratified_sample``'s convention.

    ``str(row.get(field, ""))`` — folding a **missing** key into the ``""`` stratum, which is
    what stratifying wants (it groups the missing with the genuine-empty; this is where the
    activation dataset's shared negatives, ``expected_skill: ""``, collect). The cost is that
    an explicit ``None`` becomes the string ``"None"`` rather than joining ``""``.

    Deliberately NOT :func:`row_split_label`'s convention, which treats absent / ``None`` /
    ``""`` alike. The two differ on purpose: the split filter cannot tolerate an explicit
    ``null`` silently becoming a real label, while the sampler wants missing and empty in one
    bucket. This function owns the STRATUM rule only.

    It exists as a function rather than an expression inside the sampler because
    ``coder-eval plan``'s per-stratum preview must group rows exactly the way the sampler
    draws them — a preview with its own grouping would print counts for strata the sampler
    does not use. Rendering the ``""`` key as ``(none)`` is a display concern and belongs to
    the caller, not here.
    """
    return str(row.get(field, ""))


def _stratified_sample(
    rows: list[dict[str, Any]],
    field: str,
    n: int,
    seed: int | None,
) -> list[dict[str, Any]]:
    """Randomly keep up to ``n`` rows per stratum, keyed by :func:`stratum_key`.

    Strata with <= n rows are taken whole. Output preserves first-seen stratum
    order; within a sampled stratum, rows are in their drawn (random) order.
    ``seed=None`` uses a fresh nondeterministic RNG, so the draw differs every run.
    """
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(stratum_key(row, field), []).append(row)
    out: list[dict[str, Any]] = []
    for grp in groups.values():
        out.extend(grp if len(grp) <= n else rng.sample(grp, n))
    return out


def row_split_label(row: dict[str, Any], field: str) -> str | None:
    """The row's split label, or ``None`` when the row is unlabelled.

    Unlabelled means the key is absent, ``null``, or ``""`` — the "no value here"
    convention extended to the explicit ``null`` a half-labelled JSONL carries. Any other
    value is a real label and is compared via ``str()``, so a falsy ``0`` counts as
    labelled rather than missing.

    This is the single definition of the **split-filter** convention, shared by
    ``expand_dataset`` and the lint rule that forbids a partly-labelled dataset — the one
    genuinely dangerous state, because ``--split`` keeps the matching rows and silently
    drops the unlabelled ones, so the run succeeds and every metric is computed over a
    smaller suite than the file suggests.

    Deliberately NOT the convention ``_stratified_sample`` uses: that one folds a missing
    key to the ``""`` stratum via ``str(row.get(field, ""))``, which turns an explicit
    ``None`` into the string ``"None"``. The two differ on purpose (see the note there),
    so this function owns the split-filter rule only, not a global one.
    """
    value = row.get(field)
    return None if value is None or value == "" else str(value)


def _validate_row_ids(rows: list[dict[str, Any]], task: TaskDefinition) -> None:
    """Validate every row's id against the dataset AS A WHOLE, before any narrowing.

    All three id checks live here on purpose: "the dataset is well-formed" must not
    depend on what a given invocation selected. Split the checks — duplicates whole-set,
    missing/malformed per-selected-row — and a malformed row sitting in the ``test`` split
    validates under every ``--split train`` run and surfaces only at promotion time, which
    is the most expensive moment to learn it.

    The ``Dataset row {i}`` index therefore counts over the whole dataset rather than the
    selected subset, which is the more useful number: it points at the line in the file.
    """
    assert task.dataset is not None
    id_field = task.dataset.id_field
    seen: set[str] = set()
    for i, row in enumerate(rows):
        if id_field not in row:
            raise ValueError(f"Dataset row {i} for task '{task.task_id}' missing id_field '{id_field}': {row}")
        row_id = str(row[id_field])
        if not _ROW_ID_PATTERN.match(row_id):
            raise ValueError(
                f"Dataset row id {row_id!r} must match {_ROW_ID_PATTERN.pattern}"
                + " (letters, digits, underscore, hyphen, dot)"
            )
        if row_id in seen:
            raise ValueError(f"Duplicate dataset row id for task '{task.task_id}': {row_id!r}")
        seen.add(row_id)


def _substitute_row_in_str(s: str, row: dict[str, Any]) -> str:
    """Replace ${row.<field>} occurrences in s with scalar values from row."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in row:
            raise KeyError(f"${{row.{key}}}: key not found (available: {sorted(row.keys())})")
        value = row[key]
        if isinstance(value, dict | list):
            raise TypeError(
                f"${{row.{key}}}: value must be a scalar (str/int/float/bool/None), got {type(value).__name__}"
            )
        return "" if value is None else str(value)

    return _ROW_VAR_PATTERN.sub(replace, s)


def _substitute_row_in_tree(obj: Any, row: dict[str, Any]) -> Any:
    """Walk a nested dict/list structure and substitute ${row.X} in every string leaf."""
    if isinstance(obj, str):
        return _substitute_row_in_str(obj, row)
    if isinstance(obj, list):
        return [_substitute_row_in_tree(x, row) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute_row_in_tree(v, row) for k, v in obj.items()}
    return obj


# The two causes a STRATIFIED draw reports, by source. Named here, on the producer, because a
# consumer that wants to know "was this narrowing stratified?" would otherwise sniff the cause
# string — and the two spellings differ (`--sample-per-stratum` is hyphenated, the YAML key is
# not), so a single substring test silently matches only one of them.
#
# The CLI half is DERIVED from `ROW_SELECTOR_FLAGS`, never respelled: that map is the one
# declaration of how each selector is spelled on the command line, and a `--sample-per-stratum`
# typed again here would keep printing the retired flag after a rename while CE043 stayed green.
_STRATIFIED_CLI_CAUSE = ROW_SELECTOR_FLAGS["sample_per_stratum"]
_STRATIFIED_YAML_CAUSE = "dataset.sample_per_stratum"
STRATIFIED_CAUSE_PREFIXES = (_STRATIFIED_CLI_CAUSE, _STRATIFIED_YAML_CAUSE)


class RowSelectionOutcome(NamedTuple):
    """What :func:`select_rows` selected, and which selectors actually did the narrowing.

    ``applied`` records a cause **only when it removed at least one row**, so ``--sample 99``
    over a 4-row dataset names nothing. That guard is the point: ``coder-eval plan``'s
    accounting line used to name whichever selector was *set*, which misattributed a
    YAML-driven stratified reduction to ``--split``.

    Two fields, not three. The per-stratum breakdown is
    ``Counter(stratum_key(r, field) for r in rows)`` at the display layer — carrying it here
    would be a second place to keep a derived count correct.
    """

    rows: list[dict[str, Any]]
    applied: tuple[str, ...]


def select_rows(
    rows: list[dict[str, Any]],
    dataset: Dataset,
    *,
    task_id: str,
    split: str | None = None,
    max_rows: int | None = None,
    sample_per_stratum: int | None = None,
) -> RowSelectionOutcome:
    """Apply ``--split`` then one sampler, and report which of them narrowed the set.

    **The one declaration of the selection and its precedence.** ``expand_dataset`` calls it to
    select; ``coder-eval plan`` calls it (through
    :func:`expand_dataset_with_selection`) to *preview* and prints ``applied`` verbatim. A
    ``plan`` that restated the win-order would be the very defect this consolidation fixes,
    and it would decay the same way: silently, as the order changed here.

    Order, and why:

    1. ``split`` filters FIRST. Sampling first would leave an unpredictable (possibly zero)
       number of rows per split, destroying the train/test comparison the split exists to
       protect. A task whose rows are all unlabelled passes through untouched (``--split`` is
       global to the invocation, so an unlabelled task in a multi-task run must not fail);
       partial labelling keeps the matching rows, drops the unlabelled ones and logs a
       WARNING naming the count; a *labelled* task with no matching row raises
       :class:`SplitSelectorError`.
    2. ``max_rows`` (``--sample``): flat uniform-random N over the whole dataset, fixed seed
       so it is reproducible but — unlike a first-N slice — unbiased across the concatenated
       ``dataset.paths``. When set it **overrides** both stratified sources.
    3. otherwise ``sample_per_stratum``: stratified random N-per-stratum. The CLI argument
       overrides ``dataset.sample_per_stratum`` (the YAML), so a runner can cap a dataset
       without editing its task. ``applied`` names which source supplied the count, because
       "the YAML did this" and "your flag did this" send a reader to different places.

    Row-id validation is deliberately NOT here — it is a property of the dataset, not of the
    selection, and runs over the whole row set in :func:`expand_dataset` before this is called.

    Raises:
        SplitSelectorError: A labelled dataset has no row in ``split``.
    """
    applied: list[str] = []

    if split is not None:
        field = dataset.split_field
        # One pass, one label per row: selecting and reporting read the same computed value,
        # so they cannot drift into two definitions of "labelled".
        labelled = [(r, label) for r in rows if (label := row_split_label(r, field)) is not None]
        if labelled:
            matching = [r for r, label in labelled if label == split]
            # Guarded on an actual drop, not on --split being set: a fully labelled dataset
            # drops nothing and must stay quiet, or the warning becomes noise and gets ignored.
            if len(labelled) != len(rows):
                logger.warning(
                    "Task '%s': --split %r kept %d of %d rows; %d row(s) carry no %r label and were "
                    + "DROPPED. Every metric below is computed over the smaller set.",
                    task_id,
                    split,
                    len(matching),
                    len(rows),
                    len(rows) - len(labelled),
                    field,
                )
            if not matching:
                raise SplitSelectorError(
                    f"Dataset for task '{task_id}' has no rows in split {split!r} "
                    + f"(split_field={field!r}); labelled splits present: "
                    + f"{sorted({label for _, label in labelled})}"
                )
            if len(matching) != len(rows):
                applied.append(f"{ROW_SELECTOR_FLAGS['split']} {split}")
            rows = matching
        # else: no row in this task carries a split label -> --split does not apply here.

    n_per_stratum = sample_per_stratum if sample_per_stratum is not None else dataset.sample_per_stratum
    if max_rows is not None:
        if max_rows < len(rows):
            rows = random.Random(_SMOKE_SAMPLE_SEED).sample(rows, max_rows)
            applied.append(f"{ROW_SELECTOR_FLAGS['max_rows']} {max_rows}")
    elif n_per_stratum is not None:
        # Seeded ONLY by dataset.sample_seed. When that is None the draw is deliberately
        # nondeterministic — re-drawn every run — regardless of whether the CLI flag or the
        # YAML supplied the count (see Dataset.sample_seed). The nightly activation suite
        # relies on this to broaden coverage across runs.
        sampled = _stratified_sample(rows, dataset.stratify_field, n_per_stratum, dataset.sample_seed)
        if len(sampled) != len(rows):
            # Name the SOURCE, not just the count: a reduction the task's own YAML caused
            # sends a reader to the task file, one a flag caused sends them to their command.
            if sample_per_stratum is not None:
                applied.append(f"{_STRATIFIED_CLI_CAUSE} {n_per_stratum}")
            else:
                applied.append(f"{_STRATIFIED_YAML_CAUSE}: {n_per_stratum}")
        rows = sampled

    return RowSelectionOutcome(rows=rows, applied=tuple(applied))


def expand_dataset(
    task: TaskDefinition,
    task_file_dir: Path,
    max_rows: int | None = None,
    sample_per_stratum: int | None = None,
    split: str | None = None,
) -> list[TaskDefinition]:
    """Fan out a task with ``dataset:`` into one TaskDefinition per row.

    Tasks without ``dataset:`` pass through unchanged as ``[task]``.

    Each expanded task:
      - has task_id rewritten to ``"<original_task_id>/<row_id>"``
      - has ``dataset`` cleared (prevents re-expansion downstream)
      - has ``${row.<field>}`` substituted in ``initial_prompt`` and in all
        string leaves of ``success_criteria`` entries

    Row ids are validated against a safe pattern so they're filesystem-safe
    when used as directory names under the run_dir.

    Args:
        task: Task that may carry a dataset.
        task_file_dir: Directory of the source task YAML (for resolving dataset.paths).
        max_rows: Optional CLI cap on rows used (for cheap smoke runs). A
            fixed-seed uniform-random N-row sample over the whole dataset
            (reproducible, but unbiased across ``dataset.paths`` — unlike a raw
            slice). When provided, overrides both ``sample_per_stratum`` args.
            Absent it, ``sample_per_stratum`` (stratified random) applies.
        sample_per_stratum: Optional CLI override (``--sample-per-stratum``) for
            ``dataset.sample_per_stratum`` — keep up to N rows per stratum
            (stratum = ``dataset.stratify_field``, default ``expected_skill``).
            Lets a runner cap a stratified dataset without editing the task YAML
            (the nightly activation suite uses this). Ignored when ``max_rows``
            is set. When None, falls back to ``dataset.sample_per_stratum``.
        split: Optional CLI row filter (``--split``) — keep only rows whose
            ``dataset.split_field`` value equals this. Applied BEFORE either
            sampler, so a sampled split still has a predictable size. A row is
            unlabelled when the field is absent, ``None``, or ``""``. A task
            whose rows are all unlabelled passes through unfiltered (``--split``
            is global to the invocation, so an unlabelled task in a multi-task
            run must not fail); partial labelling keeps the matching rows, drops
            the unlabelled ones and logs a WARNING naming the drop count; a
            *labelled* task with no matching row raises ``SplitSelectorError``.
            That one is NOT demoted to ``skipped_tasks`` — ``resolve_all_tasks``
            re-raises it, so a mistyped split name aborts the run instead of
            producing a green run of zero rows.

    Returns:
        Expanded list of TaskDefinitions. Length is 1 when dataset is None.

    Raises:
        ValueError: Empty dataset, duplicate row ids, missing id_field, or a
            malformed row id — all malformed-FILE errors, which
            ``resolve_all_tasks`` demotes to ``skipped_tasks``.
        SplitSelectorError: A labelled dataset has no row in ``split``. A
            ``ValueError`` subclass, but ``resolve_all_tasks`` re-raises this one
            (it describes a malformed INVOCATION, not a malformed file).
        FileNotFoundError: Dataset path does not exist.
    """
    return expand_dataset_with_selection(
        task,
        task_file_dir,
        max_rows=max_rows,
        sample_per_stratum=sample_per_stratum,
        split=split,
    )[0]


def expand_dataset_with_selection(
    task: TaskDefinition,
    task_file_dir: Path,
    max_rows: int | None = None,
    sample_per_stratum: int | None = None,
    split: str | None = None,
) -> tuple[list[TaskDefinition], RowSelectionOutcome]:
    """:func:`expand_dataset`, plus the :class:`RowSelectionOutcome` that produced it.

    The form ``coder-eval plan`` calls, so its preview reports the causes and counts from
    **the same draw** it is previewing. Two calls would re-run a nondeterministic stratified
    sampler and print a breakdown of rows the command did not return.

    Every other caller uses :func:`expand_dataset`, whose signature and return type are
    unchanged. A non-dataset task yields ``([task], RowSelectionOutcome([], ()))`` — an empty
    outcome, because a task with no ``dataset:`` has no rows to have selected.
    """
    if task.dataset is None:
        return [task], RowSelectionOutcome(rows=[], applied=())

    rows = load_dataset_rows(task.dataset, task_file_dir)
    if not rows:
        raise ValueError(f"Dataset for task '{task.task_id}' is empty")

    # Row ids are a property of the DATASET, so check the whole row set BEFORE any filtering
    # or sampling narrows it. Checking only what survives would let a malformed, id-less or
    # duplicate row sitting in an unselected split validate under every --split and surface
    # only on a full run — and the split workflow always passes one. Deliberately outside
    # select_rows for exactly that reason: validation is not part of the selection.
    _validate_row_ids(rows, task)

    outcome = select_rows(
        rows,
        task.dataset,
        task_id=task.task_id,
        split=split,
        max_rows=max_rows,
        sample_per_stratum=sample_per_stratum,
    )

    id_field = task.dataset.id_field
    expanded: list[TaskDefinition] = []

    for row in outcome.rows:
        # No validation here: all three id checks ran over the whole dataset in
        # _validate_row_ids, before filtering and sampling narrowed the set.
        row_id = str(row[id_field])

        data = task.model_dump(exclude_unset=True)
        if isinstance(data.get("initial_prompt"), str):
            data["initial_prompt"] = _substitute_row_in_str(data["initial_prompt"], row)
        if isinstance(data.get("success_criteria"), list):
            data["success_criteria"] = [_substitute_row_in_tree(c, row) for c in data["success_criteria"]]
        data["suite_id"] = task.task_id
        data["row_id"] = row_id
        data["task_id"] = f"{task.task_id}/{row_id}"
        data["dataset"] = None
        # CE041 exemption: `data` is a dump of an already-validated task with the row's fields
        # substituted in, so a dict is genuinely the input shape. Four keys just above are
        # hand-written literals rather than dump output, and `TaskDefinition` is the one target
        # without extra="forbid" — so a typo in one of THOSE would be dropped at construction
        # rather than raised. Not silently: the `mode="before"` `_warn_on_unknown_fields`
        # validator still fires, exactly as on the YAML path above.
        expanded.append(TaskDefinition(**data))  # noqa: CE041

    return expanded, outcome
