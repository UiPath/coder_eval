"""The row-selector CLI help strings, shared by ``run`` and ``plan``.

``plan`` is sold as the pre-spend preview of what ``run`` will execute, so the two commands
must offer the same selectors and describe them the same way. Both import these, which is
what stops one surface documenting a flag differently from the other. CE065 pins the pairing.

**Two strings, two jobs, neither duplicated.** ``RowSelection``'s field ``description``
documents the *recorded value* — what a reader of ``run.json`` or the schema needs. These
document the *selector action* — what someone typing the flag needs. They are not the same
sentence and must not be collapsed into one; each already has exactly one declaration and one
set of consumers, which is the property DRY actually asks for.

The field -> flag **mapping** is deliberately NOT here: it is ``ROW_SELECTOR_FLAGS`` on
``models/row_selection.py``, because ``reports.py`` renders those flag names too and a report
module importing from ``coder_eval.cli`` is the upward dependency this repo's layering forbids.
"""

SPLIT_HELP = (
    "For dataset-backed tasks, keep only rows whose dataset.split_field value "
    "(default field: split) matches this name — e.g. --split train / --split test. "
    "Applied BEFORE --sample / --sample-per-stratum, so a sampled split keeps a "
    "predictable size. Tasks whose rows are all unlabelled are unaffected; a labelled "
    "task with no row in this split is an error naming the splits that exist."
)

SAMPLE_HELP = (
    "For dataset-backed tasks, use a random N-row sample over the whole dataset "
    "(fixed seed: reproducible, unbiased across dataset.paths). Cheap dataset "
    "smoke-test. Overrides --sample-per-stratum when both are given."
)

SAMPLE_PER_STRATUM_HELP = (
    "For dataset-backed tasks, keep up to N rows per stratum (stratify_field, "
    "default expected_skill) — a stratified sample that overrides the task's "
    "dataset.sample_per_stratum without editing the YAML. Ignored when --sample is set. "
    "Nondeterministic (re-draws each run) unless the task sets dataset.sample_seed."
)
