"""The row-selection provenance record — which subset of a dataset a run actually executed.

A cycle-free leaf (pydantic only), the same role ``models/judge_defaults.py`` and
``models/optimize.py``'s ``TARGET_LABEL`` already play. It is embedded by both
``orchestration.config.BatchRunConfig`` (the request) and ``models.results.RunSummary``
(the record), which is what makes a train run and a test run of the same suite
distinguishable downstream instead of identical.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RowSelection(BaseModel):
    """The three dataset row selectors, declared once.

    Requested on the command line as ``--split`` / ``--sample`` / ``--sample-per-stratum``
    (see :data:`ROW_SELECTOR_FLAGS`), recorded into ``run.json`` so a consumer can tell
    *which rows* a run scored. Two runs of one suite over different splits are otherwise
    byte-indistinguishable in every downstream artifact, and pairing them is a comparison
    of two different row sets reported as one measurement.

    **Deliberately no ``extra="forbid"``, and that is a trade rather than an oversight.**
    This model is nested under a container that forbids extras (``BatchRunConfig``) *and*
    under one that deliberately does not (``RunSummary``, whose whole module is exempt from
    CE009 to keep ``run.json`` / ``task.json`` forward-compatible). Forward compatibility
    wins: ``reports.py`` and ``reports_junit.generate_junit_xml`` both call
    ``RunSummary.model_validate_json`` on run directories that may have been written by a
    *newer* coder-eval (runs are pulled from blob storage), and a ``forbid`` here would turn
    a future fourth selector into a hard parse failure of the entire report rather than an
    ignored key. The config-side typo risk is covered statically instead — ``RowSelection(splt=...)``
    is a pyright error, and the model is constructed at exactly one production site with
    literal keywords.
    """

    split: str | None = Field(
        default=None,
        description=(
            "The ``--split`` value this run selected on: only dataset rows whose "
            "``dataset.split_field`` value matched were executed. ``None`` means no split "
            "selector was applied, so the run covered every row that survived the samplers. "
            "Tasks whose rows carry no split label are unaffected by the selector either way."
        ),
    )
    max_rows: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The ``--sample N`` value: a fixed-seed uniform-random N-row draw over the whole "
            "dataset (reproducible run to run). ``None`` means no flat sample was requested. "
            "When set it overrides ``sample_per_stratum``."
        ),
    )
    sample_per_stratum: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The ``--sample-per-stratum N`` value: keep up to N rows per stratum "
            "(``dataset.stratify_field``, default ``expected_skill``). ``None`` means the CLI "
            "requested no stratified cap — the task's own ``dataset.sample_per_stratum`` may "
            "still have applied, which this field does NOT record. Ignored when ``max_rows`` is set."
        ),
    )

    @property
    def requested(self) -> bool:
        """True when at least one selector was ASKED for.

        Deliberately not called ``narrowed``: whether a selector actually removed a row is a
        comparison of counts this model does not carry (``--sample 99`` over a 4-row dataset
        requests a sample and narrows nothing). The run report and the JUnit properties print
        on this; ``coder-eval plan``'s accounting line and the optimize gate ask different
        questions and must not read a subset-claim into it.
        """
        return self.split is not None or self.max_rows is not None or self.sample_per_stratum is not None


# The flag each field is spelled as on the command line. It lives HERE, on the leaf, rather than
# under ``cli/``: ``reports.py`` renders these names too, and a top-level report module importing
# from ``coder_eval.cli`` is the upward dependency CE004 forbids inside core (and that the
# ``leak_detection.py`` rationale in CLAUDE.md forbids by argument even where CE004 does not
# reach). The mapping is explicit rather than string-munged because one name genuinely differs:
# the field is ``max_rows``, the flag is ``--sample``.
ROW_SELECTOR_FLAGS: dict[str, str] = {
    "split": "--split",
    "max_rows": "--sample",
    "sample_per_stratum": "--sample-per-stratum",
}
