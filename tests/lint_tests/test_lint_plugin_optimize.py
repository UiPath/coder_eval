"""Lint tests: `optimize-skill`, the method file, the proposal prompt, and the snippet binder behind them."""

import pathlib
import re
from pathlib import Path

import pytest

from tests.lint_tests.plugin_base import PluginArtifactsBase
from tests.lint_tests.shared import (
    _NORMALIZED_IMPL,
    _PROPOSAL_TOKENS,
    _SKILL_PROCEDURE_TOKENS,
    PLUGIN_ROOT,
    TESTS_ROOT,
    _missing_tokens,
    _normalized,
    _snippet_binding_failures,
    _wrong_skill_count_offenders,
)


@pytest.mark.lint
class TestTheOptimizeSkillSurfaces(PluginArtifactsBase):
    """`optimize-skill`, the method file, the proposal prompt, and the snippet binder behind them.

    One of five classes carved out of `TestPluginArtifacts`; the shared class attributes and
    grader helpers live on :class:`PluginArtifactsBase`.
    """

    def test_optimize_skill_keeps_its_load_bearing_instructions(self):
        # optimize-skill's correctness lives entirely in its prose: it drives paid runs and
        # every one of these instructions is something a well-meaning edit would "simplify"
        # away, leaving a skill that still reads plausibly and measures nothing. Same
        # deletion-sensor shape as the lint-tasks read-only guard above.
        # TWO surfaces, and which one a token belongs to is the reviewable part of the
        # split. `SKILL.md` holds the PROCEDURE — which suite, which files, which commands,
        # in what order — so a procedure token asserted anywhere else would mean the step
        # that needs it no longer states it. `reference/optimize-method.md` holds the
        # track-invariant METHOD — the cost table, what each stage bounds, why the two gates
        # differ, the sign rule — which is identical on both tracks and is what has to be
        # right for a verdict to mean anything. Tokens that may legitimately live in either
        # are asserted against the concatenation.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        text = skill + " " + method

        # The single most important invariant. Suite rollups pool replicates, so --repeats
        # writes ONE pooled suite.json and the per-replicate F1 the Stage B gate reads would
        # not exist. Collapsing three invocations into --repeats 3 is the likeliest edit,
        # and it fails silently — the gate would compare a number against itself.
        assert "not** `--repeats 3`" in text or "**not** `--repeats`" in text, (
            "optimize-skill no longer says Stage B must be THREE SEPARATE INVOCATIONS rather "
            "than --repeats 3 — suite rollups pool replicates, so --repeats yields one pooled "
            "suite.json with no per-replicate F1 for the gate to read"
        )
        assert "keyed on `(variant, suite)`" in text, (
            "optimize-skill states the not---repeats rule but no longer says WHY (the rollup "
            "grouping key) — a rule with no reason attached is the first thing an editor drops"
        )

        for token, why in (
            ("--split train", "the train split drives proposals and the gate"),
            ("--split test", "the test split is what makes a promotion more than a fit"),
            ("--split test --repeats 3", "Stage C's paired comparison needs replicates averaged per row"),
            ("stop_early", "an armed suite can pass-stop before a sibling misfire is observable"),
            ("agent.plugins", "reachability wiring — the mechanism, not an invented one"),
            ("recall 0.0", "the silent failure mode of a wrong plugin path"),
            ("disable-model-invocation", "the hard stop on a skill whose description never triggers"),
            ('metrics["f1.yes"]', "F1 is read from the rollup, never recomputed"),
            ("process working directory", "plugin paths resolve against the process cwd, not the task file's dir"),
            # Annexation shows up as a sibling FALSE NEGATIVE, so it moves recall, not
            # precision — on a suite where the sibling never misfires, precision.yes is
            # pinned at 1.0 and a precision gate would be gating on a constant.
            ("**`recall.yes`**", "the sibling-regression gate must read recall, not precision"),
            # Step 7's snapshot must carry the siblings: a variant's plugins block REPLACES
            # the task's, so the round-slug dir is the arm's only skill source. Snapshot one
            # skill and every sibling criterion silently observes `no` in every arm.
            ("copied unchanged", "each arm's snapshot must include the sibling skills, not just the target"),
            # The execution track measures the BODY, and an activation suite cannot grade a
            # body — skill_triggered scores engagement only. Losing this collapses the two
            # tracks onto one instrument that answers the wrong question.
            ("wrong instrument", "the execution track must refuse to reuse an activation suite"),
            # Inverse of the activation rule, and easy to "correct" into a bug: the body
            # track invokes the skill from the prompt to hold activation constant, so what
            # varies is the body alone.
            ("Invoke the skill from the prompt", "the execution track holds activation constant by invoking the skill"),
            # And it must be the SLASH form. Verified live: a `disable-model-invocation`
            # skill is not offered to the model at all, so asking in prose returns "no such
            # skill available" and the row measures nothing — while `/plugin:skill` in the
            # prompt loads it, emits a real Skill tool call, and is detected by
            # skill_triggered. Prose-instead-of-slash is the intuitive edit and it is silent.
            ("Use the slash form", "prose cannot reach a disable-model-invocation skill; only the slash form loads it"),
            # One variable per round. A body edit shipped alongside a description edit is
            # unattributable, and the description change also moves activation.
            ("Never run both tracks in one round", "tracks must not be combined in a single measurement"),
            # A skill that cannot be model-invoked still has an optimizable body — routing
            # to the execution track rather than stopping is the point of the two-track split.
            ("route to the execution track", "disable-model-invocation must route to the body track, not hard-stop"),
            # The whole execution track is predicated on this. suite.json is written only for
            # tasks the dataset expander touched (rollups group on suite_id, which nothing
            # else sets) and --split filters dataset ROWS — so a directory of separate task
            # files gives Stage A no rollup to rank and makes Stage C's --split test silently
            # re-run the train rows. "One task per scenario" is the intuitive shape and it is
            # the broken one.
            (
                "ONE dataset-backed task",
                "the outcome suite must be one dataset-backed task or Stage A has no rollup and Stage C re-runs train",
            ),
            (
                "silently re-runs the train rows",
                "the consequence that makes the dataset requirement non-optional, not a preference",
            ),
            # expand_dataset copies the SAME success_criteria onto every row, substituting
            # ${row.*} into every string leaf. Per-scenario assertions are therefore
            # parameterized by row fields; writing different criteria per scenario is
            # unrepresentable, and discovering that after authoring the rows is expensive.
            (
                "Criteria are copied to every row",
                "per-scenario assertions must be parameterized by row fields, never written per scenario",
            ),
            # Substitution reaches initial_prompt and success_criteria ONLY, never
            # sandbox.template_sources — so a suite has exactly one fixture and scenario
            # variation has to live in the prompt.
            (
                "Every row shares ONE sandbox fixture",
                "row substitution never reaches sandbox:, so rows needing different repo shapes are two suites",
            ),
            (
                "preconditions the skill under test checks",
                "a fixture that fails the skill's own hard stop ties every arm at zero and reads as bad candidates",
            ),
            # There is no --variant flag, so the arm set changes by AUTHORING A NEW FILE.
            # Re-passing the triage file at Stage B/C costs (N+1)/2x the budgeted runs and
            # renders no paired block at all, with nothing in the output announcing it.
            (
                "no `--variant` filter",
                "the arm set can only be changed by authoring a per-stage experiment file",
            ),
            (
                "no `## Paired Comparison` block at all",
                "the cost of re-passing the triage file at Stage B/C — more spend, strictly less evidence",
            ),
            (
                "round<N>-triage.yaml",
                "Stage A's own experiment file — the per-stage naming Steps 9/10 and the ledger depend on",
            ),
            (
                "round<N>-gate.yaml",
                "Stage B's own experiment file, which on the execution track must hold exactly two variants",
            ),
            (
                "round<N>-confirm.yaml",
                "Stage C's own experiment file — reusing the gate or triage file is the documented mistake",
            ),
            (
                "because that block fires only for exactly two variants",
                "the REASON the per-stage files matter: paired_comparison's exactly-two precondition",
            ),
            # The P1-5 mitigation: the skill supplies the artifact rather than relaying
            # requirements to /coder-eval:task, which come back half-applied.
            (
                "reference/templates/outcome.yaml",
                "the execution track must hand over the bundled outcome template, not point vaguely at one",
            ),
            (
                "Hand over the template itself",
                "relayed requirements come back half-applied and the user pays for a second round trip",
            ),
            # Step 5: the common path is that the suite is ALREADY labelled, so the
            # unlabelled branches are the exception rather than the expected work.
            (
                "arrives labelled",
                "a check-skill suite already carries splits — the labelling branches are for hand-authored ones",
            ),
            # A body naming a tool outside the allowlist fails identically in every arm and
            # reads as "the body is bad". The activation track cannot have this confound.
            (
                "tool policy constant across arms",
                "an unpinned tool policy makes a body that names a disallowed tool look like a bad body",
            ),
            # Per-row cost brake. Without it a runaway row eats a whole stage's budget.
            ("run_limits.max_usd", "the per-row cost brake on an outcome suite, where every row is a full task run"),
            # The inverse of the activation guidance. An activation suite caps max_turns at 2
            # deliberately; carried over, an outcome row is truncated and scores as a body
            # failure — a fabricated result, since the body was never allowed to finish.
            # Anchored on the plain-prose consequence rather than the emphasized phrase: a
            # sensor that depends on Markdown bold breaks on a rewrap without the
            # instruction having been touched, which trains readers to distrust it.
            (
                "which is a fabricated result",
                "carrying an activation suite's tight caps to an outcome suite fabricates body failures",
            ),
            # The activation gate's instrument. It replaced `min(candidate F1) > max(incumbent F1)`,
            # which discarded the pairing (both arms ran the SAME rows) and had very little power at
            # 8-12 rows per polarity. Resampling ROWS is the load-bearing word: replicates within a
            # row are the same request asked again, so resampling them individually would understate
            # the interval and manufacture separation.
            ("cluster bootstrap", "the activation gate's instrument — resampling rows, not observations"),
            ("excludes zero", "the promotion condition; a CI containing zero is a non-result"),
            (
                "minimum detectable effect",
                "Stage A must price the smallest resolvable difference before spending, or a stage that "
                "cannot see the effect returns a non-result indistinguishable from a real one",
            ),
            ("Holm", "the Stage A -> Stage B multiplicity correction"),
            (
                "reported diagnostic",
                "range non-overlap is retained as a diagnostic and must never be restored as the gate",
            ),
            # The cost guardrail, named for what it actually compares. A body edit that doubles what
            # a row costs for +0.02 F1 is a trade, not a win.
            ("median cost per row", "the cost guardrail — a body edit that doubles spend must not promote silently"),
            ("median latency per row", "the latency half of the same guardrail"),
            # The refusal rule. Without it the tool reports "not promoted" on a suite where no
            # candidate could ever promote — the same overclaim UNDECIDED exists to prevent, one
            # state further along.
            (
                "REFUSES rather than reporting a negative result",
                "the method file must state that a floor above the Holm threshold is a refusal, "
                "not an ordinary negative result",
            ),
            (
                "add rows",
                "a refusal's remedy is more rows or a smaller family — never another round on the "
                "same suite, which reproduces the floor exactly",
            ),
            # Two preflights, not one. The method file said the execution track gets none; it now
            # gets one on its own metric, from data the control stage already pays for.
            (
                "Two preflights",
                "the method file must describe BOTH tracks' noise floors — it previously said the "
                "execution track gets none, which is the claim this phase made false",
            ),
        ):
            assert token in text, f"optimize-skill lost {token!r} — {why}"

        # The now-false claim, asserted ABSENT. A sensor that only checks the replacement is
        # present would stay green if both sentences shipped side by side, which is worse than
        # either alone: the reader gets two contradictory instructions and no way to pick.
        assert "Not on the execution track" not in method, (
            "reference/optimize-method.md still says the execution track gets no preflight. It gets "
            "one — on weighted_score, after the control arm — and the two claims cannot both stand."
        )

        # Every PROCEDURE token, checked through the same matcher the self-test exercises.
        missing = _missing_tokens(skill, _SKILL_PROCEDURE_TOKENS)
        assert not missing, (
            "optimize-skill's SKILL.md lost:\n  "
            + "\n  ".join(missing)
            + "\n\nThese are PROCEDURE: they must stay in the skill, not move to "
            "reference/optimize-method.md"
        )
        # An extracted reference nothing points at is a deleted reference. Mirrors the
        # reference/task-rubric.md pointer sensor.
        assert "${CLAUDE_PLUGIN_ROOT}/reference/optimize-method.md" in skill, (
            "optimize-skill's SKILL.md no longer points at reference/optimize-method.md, so the "
            "cost table, the gate rules and the sign rule are unreachable from the procedure "
            "that has to apply them"
        )

        # The METHOD half, asserted against the method file alone — what a stage BOUNDS is
        # track-invariant and is the thing that has to be right for a verdict to mean anything.
        for token, why in (
            (
                "search, not a gate",
                "the search loop bounds nothing: its comparison is across invocations, unpaired "
                "and unbootstrapped, so a search win is a hypothesis to gate and never a promotion",
            ),
        ):
            assert token in method, (
                f"reference/optimize-method.md lost {token!r} — {why}. This is METHOD: it must stay "
                f"in the reference, not move to the skill"
            )

        # The paired-diff sign rule has to appear in BOTH gates that read the block —
        # Stage B (execution) and Stage C — because each is a separate decision point and a
        # reader lands on one or the other. Counted rather than `in text`: a presence check
        # stays green when one of the two is deleted, which is exactly the edit to catch.
        # The sign follows VARIANT DECLARATION ORDER (reports_stats builds vid_a/vid_b from
        # variant_ids[0]/[1]), so with `incumbent` declared first a candidate win is NEGATIVE
        # — and a reversed reading promotes the arm that lost, with every later number in the
        # ledger corroborating it.
        # Counted against the METHOD file, because both decision points moved there together
        # (Stage B and Stage C are adjacent sections of it). The reason for TWO is unchanged
        # by the move: each stage is a separate decision point a reader lands on
        # independently, so a cross-reference from one to the other is not good enough.
        for phrase, expected in (("variant declaration order", 2), ("a candidate win reads negative", 2)):
            assert method.count(phrase) >= expected, (
                f"reference/optimize-method.md states {phrase!r} {method.count(phrase)} time(s); both "
                f"the execution Stage B gate and Stage C must carry the paired-diff sign rule, so it "
                f"needs {expected}"
            )

        # The cost table's symbols must match the prose that defines them. It shipped
        # reading `M_tune`/`M_holdout` after the split values were renamed to train/test —
        # invisible to every sensor above, because the rename deleted no instruction, and
        # the table is the surface a reader budgets from.
        for stale in ("M_tune", "M_holdout"):
            assert stale not in text, (
                f"optimize-skill's cost table still uses {stale!r} — the splits are `train`/`test`, "
                "so the table names symbols the surrounding prose never defines"
            )
        for symbol in ("M_train", "M_test"):
            assert symbol in text, f"optimize-skill's cost table lost {symbol!r}"

        # The two gates use DIFFERENT machinery, on purpose, and the reason is subtle enough
        # that a well-meaning edit would unify them. Activation compares F1, which the pooled
        # suite.json cannot report per replicate -> three invocations. Execution compares
        # per-row weighted_score, which `paired_comparison` already computes correctly over
        # replicates -> `--repeats 3` on exactly two variants. Collapsing either into the
        # other silently swaps the instrument for one that cannot see the metric.
        assert "primary instrument" in text, (
            "optimize-skill no longer says the paired comparison is the PRIMARY instrument on "
            "the execution track — it is only corroboration on the activation track, and the "
            "difference is what makes each gate valid for its own metric"
        )
        assert "deliberate departure" in text, (
            "optimize-skill no longer flags that the execution gate departs from the "
            "activation gate on purpose — unlabelled, the two look like an inconsistency to "
            "be tidied away"
        )

        # Its sibling's real name. `/coder-eval:skill-check` is a dangling command, and three
        # steps plus several edge cases hand control back to check-skill.
        assert "/coder-eval:check-skill" in text, "optimize-skill lost its pointer to /coder-eval:check-skill"
        assert "skill-check" not in text.replace("check-skill", ""), (
            "optimize-skill names `skill-check` — that command does not exist; the sibling is `check-skill`"
        )

    def test_optimize_method_documents_successive_halving(self):
        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        text = method + " " + skill

        for token, why in (
            # Capitalized as both surfaces write it — these sensors match the file, not a
            # normalized form, so a rewording is visible rather than silently tolerated.
            ("Successive halving", "the two-pass Stage A that stops paying full price for doomed arms"),
            (
                "dataset.sample_seed",
                "an unpinned seed makes Stage B's three invocations draw three different row sets, "
                "and the gate then pairs almost nothing while reporting an interval anyway",
            ),
            (
                "across invocations",
                "the hazard is ACROSS invocations, not within one — within a single invocation every "
                "arm sees the same rows by construction, so a sensor that lost this would let the "
                "warning be re-described as a within-stage problem and guard the wrong thing",
            ),
        ):
            assert token in text, f"optimize-skill lost {token!r} — {why}"

        # The seed rule is PROCEDURE — it is a thing to check before running a stage — so it must
        # survive in SKILL.md specifically. Asserted against the pair above, deleting the Step 10
        # block would stay green on the method file's copy alone.
        for token in ("dataset.sample_seed", "across invocations"):
            assert token in skill, (
                f"optimize-skill's SKILL.md lost {token!r}. This is PROCEDURE: the agent has to act "
                f"on it before spending a stage, so it cannot live only in the method file."
            )

        # The halving row must be priced in the same symbols as every other row.
        assert "M_train/2" in method, (
            "reference/optimize-method.md's cost table lost the halved Stage A line, so a reader "
            "cannot see what halving actually saves before choosing it"
        )
        assert "by construction" in method, (
            "reference/optimize-method.md no longer says arms share rows BY CONSTRUCTION — without "
            "it a reader guards the seed against a risk that does not exist and misses the real one"
        )

    def test_optimize_skill_documents_the_no_skill_control(self):
        # The control answers the question every later round assumes the answer to. Losing it
        # means rounds of wording changes measured against a body that does nothing here.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")

        # Asserted per SURFACE, not against the concatenation: both files carry the control today,
        # so a pair-wide check would stay green if the whole Step 8 subsection were deleted — and
        # the procedure is the half an agent actually executes.
        for surface, name in ((skill, "SKILL.md"), (method, "reference/optimize-method.md")):
            for token, why in (
                ("control arm", "the arm that establishes the body does measurable work at all"),
                (
                    "body is emptied",
                    "removing the skill instead changes the LISTING, so the control would differ in "
                    "two ways at once and attribute neither — the intuitive simplification is wrong",
                ),
                (
                    "once per suite",
                    "the control is a property of the suite, not the round; re-running it every "
                    "round is pure spend, and the cost table prices it as a one-off",
                ),
            ):
                assert token in surface, f"optimize-skill's {name} lost {token!r} — {why}"

        # `round<N>-control.yaml` is named for the round, unlike every other per-stage file, and
        # that reads as "author one per round" unless the skill says otherwise. Asserted against
        # SKILL.md specifically, because Step 9's file list is where an agent decides what to write.
        assert "authored **once per suite**" in skill or "once per suite** rather than" in skill, (
            "optimize-skill's SKILL.md lists round<N>-control.yaml among the per-stage experiment "
            "files without saying it is authored ONCE PER SUITE. Its round-numbered name reads "
            "like its neighbours, which are per-round, so the distinction has to be explicit."
        )

    def test_optimize_skill_points_at_the_proposal_prompt(self):
        # An extracted reference nothing points at is a deleted reference. Mirrors the
        # optimize-method.md and task-rubric.md pointer sensors.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        assert "${CLAUDE_PLUGIN_ROOT}/reference/proposal-prompt.md" in skill, (
            "optimize-skill's SKILL.md no longer points at reference/proposal-prompt.md, so the "
            "shape of the proposal — the trajectories, the anti-repetition history and the test "
            "blinding — is unreachable from the step that has to generate candidates"
        )

    def test_proposal_prompt_keeps_its_load_bearing_instructions(self):
        # This file exists to stop specific failures, each invisible in the output: a proposer that
        # paraphrases its last attempt, one that has seen the test split, one that fixes the row in
        # front of it rather than the category it belongs to, one that never opens the reference
        # solution the suite ships, and one whose edits are exhortations rather than techniques.
        text = _normalized(PLUGIN_ROOT / "reference" / "proposal-prompt.md")
        missing = _missing_tokens(text, _PROPOSAL_TOKENS)
        assert not missing, "reference/proposal-prompt.md lost:\n  " + "\n  ".join(missing)

    @pytest.mark.parametrize(
        ("surface", "tokens"),
        [
            ("reference/proposal-prompt.md", _PROPOSAL_TOKENS),
            ("skills/optimize-skill/SKILL.md", _SKILL_PROCEDURE_TOKENS),
        ],
        ids=["proposal-prompt", "optimize-skill"],
    )
    def test_the_token_matcher_catches_a_removed_sentence(self, surface: str, tokens):
        """The self-test: the REAL matcher, run against text with a guarded sentence removed.

        The committed replacement for "delete it locally and check the test goes red" — which
        proves nothing about the sensor a month later. **Every** token is exercised, in both
        registries, so a pair that can never fail is caught here rather than shipping as
        decoration: that is the failure class this whole file exists to prevent, and a deletion
        sensor is the easiest place in the repo to introduce one.
        """
        text = _normalized(PLUGIN_ROOT / surface)
        assert _missing_tokens(text, tokens) == []

        for token, _why in tokens:
            gutted = text.replace(token, "")
            assert gutted != text, f"{token!r} is not actually present in {surface} — the sensor is decoration"
            missing = _missing_tokens(gutted, tokens)
            assert any(repr(token) in entry for entry in missing), (
                f"removing {token!r} from {surface} did not make the matcher report it"
            )

    def test_optimize_skill_snippet_names_the_public_gate_api(self):
        # A prose sensor cannot see a snippet drifting from the API it calls: the tokens stay
        # present, the skill stays readable, and the step fails at runtime in the user's terminal
        # after they have paid for three invocations. So assert both halves — the names are in the
        # skill AND they are importable from the module the skill tells the user to import.
        import importlib

        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")

        # Derived from the skill's own import lines rather than a list here, so a snippet that
        # starts importing something new is covered without anyone remembering to extend this.
        #
        # EVERY `coder_eval` module, not just `optimize.gate`: the snippets also import
        # `expand_dataset` / `load_task` from `coder_eval.orchestration.task_loader` and models
        # from `coder_eval.models`, and a rename there fails in the user's terminal after the runs
        # are paid for — which is precisely the failure this sensor's own comment describes.
        imported: dict[str, set[str]] = {}
        for module, block in re.findall(r"from (coder_eval[\w.]*) import \(([^)]*)\)", raw):
            imported.setdefault(module, set()).update(n.strip().rstrip(",") for n in block.split() if n.strip(" ,"))
        for module, line in re.findall(r"from (coder_eval[\w.]*) import ([^(\n]+)", raw):
            imported.setdefault(module, set()).update(n.strip() for n in line.split(",") if n.strip())

        # ONE module now, and that is the claim rather than a floor: the skill's surface is
        # `optimize.api` and nothing else, so every guard, fallback and track branch it used to
        # spell in markdown is in code where a test can reach it. This replaced `len(family) >= 4`,
        # which was right while the snippets reached into five decision modules and became
        # unfailable the moment they stopped.
        family = {m for m in imported if m.startswith("coder_eval.optimize.")}
        # KEPT alongside CE066 rather than replaced by it: this one reads the REAL shipped file as
        # part of the surface suite, while CE066's reader is reusable and carries the synthetic
        # positives. Neither subsumes the other, and a rule with no synthetic positive is a rule
        # nobody has watched fail.
        assert family == {"coder_eval.optimize.api"}, (
            f"optimize-skill's SKILL.md imports from {sorted(family)} — the declared surface is "
            "`coder_eval.optimize.api` alone. A fence reaching past it is a fence not finished."
        )
        for module, names in sorted(imported.items()):
            loaded = importlib.import_module(module)
            for name in sorted(names):
                assert hasattr(loaded, name), (
                    f"optimize-skill's SKILL.md tells the user to import {name!r} from "
                    f"{module}, which no longer exports it — the snippet would fail at "
                    f"runtime, after the user has paid for the runs it was meant to read"
                )

        # The ones the procedure must NAME, even if a future snippet stops importing them inline.
        # This hardcoded list is the REAL guard: the derived half above only asserts that whatever
        # the skill imports exists, so deleting a whole snippet leaves it green on the others.
        # `candidate_leaks` was here until the leak preflight became a composite: the procedure no
        # longer names the primitive, it names `leak_report`, which is what a reader has to run. The
        # other four survive in PROSE and keep their guard for that reason.
        # EXTENDED, not replaced. The first four are library names the PROCEDURE must keep naming even
        # though no fence imports them any more — the prose is where a reader learns what the composite
        # is doing under the block. The rest are the composites the procedure has to name because they
        # are what a reader runs; a step whose composite went unnamed would be a step nobody can find.
        for name in (
            "activation_gate",
            "holm_promote",
            "render_markdown",
            "search_compare",
            "activation_gate_report",
            "confirm_report_activation",
            "execution_gate_report",
            "leak_report",
            "record_round_execution",
        ):
            # WORD-BOUNDED, because four of these names are PREFIXES of composites in the same list:
            # a plain `"activation_gate" in skill` is satisfied by `activation_gate_report` alone, so
            # the library name could vanish from the prose while the sensor stayed green.
            assert re.search(rf"\b{re.escape(name)}\b", skill), (
                f"optimize-skill's SKILL.md no longer names {name!r} in its procedure"
            )

    def test_optimize_skill_snippets_parse_and_bind(self):
        """The third half: a snippet's CALLS must still bind against the real signatures.

        `test_optimize_skill_snippet_names_the_public_gate_api` asserts the imported names exist.
        A renamed or removed keyword ARGUMENT is invisible to it — the name resolves, the tokens
        are all present, and the snippet raises `TypeError` in the user's terminal after they have
        paid for three invocations.

        **Boundary**, so a green run is not mistaken for a proof:

        - KEYWORD arguments only. A snippet's positional values are placeholders (`run_dir`,
          `dirs`), and `bind_partial` inspects names rather than values, so nothing here executes
          or resolves a snippet local.
        - A callee taking `**kwargs` accepts anything, so the sensor is silent there by
          construction.
        - A name assigned anywhere in the same fence shadows the import, and is skipped — binding
          a local's call against the imported function's signature would be simply wrong.
        """
        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        failures = _snippet_binding_failures(raw)
        assert not failures, "\n  ".join(["optimize-skill's snippets no longer bind:", *failures])

    def test_the_snippet_binder_reads_the_real_snippets(self):
        """Anti-vacuity, on the REAL file: a binder that found no calls would also pass green.

        Mutating one keyword the shipped snippets actually pass must light up several fences.
        """
        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        mutated = raw.replace("suite_id=", "suite_i=")
        assert mutated != raw, "the anchor moved — re-derive it from the skill's snippets"

        failures = _snippet_binding_failures(mutated)
        # An EXACT count, measured against the real file. A floor was right while there were fifteen
        # multi-line fences to disagree about; after the migration each fence is one call, so a floor
        # of 5 is unfailable in practice and would not notice ten of them going quiet.
        assert len(failures) == 13, failures
        assert all("suite_i" in f for f in failures), failures

    def test_the_snippet_binder_catches_a_bogus_keyword(self):
        # The self-test. Without it the binder could be reverted to a no-op with everything green.
        markdown = (
            "```python\n"
            "from coder_eval.optimize.activation import activation_gate\n\n"
            "activation_gate(suite_id='s', bogus_kwarg=1)\n"
            "```\n"
        )
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "bogus_kwarg" in failures[0], failures

    def test_the_snippet_binder_catches_a_moved_name(self):
        """The hole the call loop leaves, hole 1: a moved name resolves to `None`.

        The call loop skips it via `if not callable(target): continue`, so a snippet importing a
        name that no longer exists reported NOTHING. This is the sensor the module split depends
        on — every one of the skill's imports is about to change module.
        """
        markdown = (
            "```python\n"
            "from coder_eval.optimize.activation import a_name_that_moved\n\n"
            "a_name_that_moved(suite_id='s')\n"
            "```\n"
        )
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1, failures
        assert "a_name_that_moved" in failures[0] and "has no attribute" in failures[0]

    def test_the_snippet_binder_catches_a_moved_name_that_is_never_called(self):
        """The hole the call loop leaves, hole 2, and it is independent of the first.

        The loop only visits names used as `ast.Call` funcs, so a name imported for a type
        annotation or a constant is invisible to it however broken. `CostQualityPoint`,
        `SearchComparison`, `TASK_JSON_GLOB`, `GATE_RESAMPLES` and `MATERIALITY_FLOOR` are all
        imported-not-called in the shipped skill, which is why the check runs over the import map.
        """
        markdown = "```python\nfrom coder_eval.optimize.activation import A_CONSTANT_THAT_MOVED\n```\n"
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "A_CONSTANT_THAT_MOVED" in failures[0], failures

    def test_the_snippet_binder_catches_a_nonexistent_module(self):
        # `import_module` RAISES here; reporting rather than propagating keeps one bad fence from
        # taking every other snippet's check down with it.
        markdown = "```python\nfrom coder_eval.nonexistent import thing\n```\n"
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "does not import" in failures[0], failures

    def test_a_nonexistent_module_whose_name_is_also_called_is_reported_not_raised(self):
        """The crash the call loop used to produce, and the reason this sensor exists at all.

        With a call present, the loop reached its own `import_module` — unguarded — and a mistyped
        module name raised `ModuleNotFoundError` straight out of the sensor instead of reporting a
        failure. Found by simulating the module split against the real `SKILL.md`: every import
        re-pointed at a module that does not exist yet, which is exactly what a half-finished
        Phase 7 looks like.
        """
        markdown = (
            "```python\nfrom coder_eval.optimize_nonexistent import activation_gate\n\n"
            "activation_gate(suite_id='s')\n```\n"
        )
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "does not import" in failures[0], failures

    def test_the_binder_reports_rather_than_crashes_on_a_wholesale_module_rename(self):
        """The Phase 7 rehearsal, on the REAL file: the sensor must survive being wrong."""
        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        mutated = raw.replace("from coder_eval.optimize.", "from coder_eval.nowhere_optimize.")
        assert mutated != raw, "the anchor moved — re-derive it from the skill's snippets"
        failures = _snippet_binding_failures(mutated)
        # Exact, for the reason the sibling above gives: one import line per fence now.
        assert len(failures) == 16, failures
        assert all("does not import" in f for f in failures), failures

    def test_a_real_non_callable_import_stays_silent(self):
        """`UNRESOLVED_MODEL` and `UNRECORDED_SPLIT` are strings the shipped skill imports.

        The call loop's `not callable` skip must survive: it exists for exactly these, and the new
        existence check must not start reporting them.
        """
        markdown = "```python\nfrom coder_eval.optimize.store import UNRESOLVED_MODEL, UNRECORDED_SPLIT\n```\n"
        assert _snippet_binding_failures(markdown) == []

    @pytest.mark.parametrize(
        "markdown",
        [
            "```python\nfrom coder_eval.optimize.load import (\n    load_arm_rows,\n    gone_missing,\n)\n```\n",
            "```python\nfrom coder_eval.optimize.load import load_arm_rows, gone_missing\n```\n",
        ],
        ids=["parenthesized", "single-line"],
    )
    def test_both_import_forms_are_covered(self, markdown):
        # `origins` is built by two separate regexes; a check that only saw one form would go
        # silent on half the skill's imports.
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "gone_missing" in failures[0], failures

    def test_the_snippet_binder_skips_a_shadowed_name(self):
        # A snippet that rebinds an imported name locally must not be bound against the import.
        markdown = (
            "```python\n"
            "from coder_eval.optimize.activation import activation_gate\n\n"
            "activation_gate = lambda **kw: None\n"
            "activation_gate(bogus_kwarg=1)\n"
            "```\n"
        )
        assert _snippet_binding_failures(markdown) == []

    @pytest.mark.parametrize(
        "surface",
        [
            "skills/optimize-skill/SKILL.md",
            "reference/optimize-method.md",
        ],
    )
    def test_no_surface_revives_a_retired_guardrail_claim(self, surface: str):
        """A retired claim must stay retired — the ABSENCE half this sensor family lacked.

        `promoted` folds both veto lists on both tracks now, so any prose telling a reader that
        the guardrails gate "in the procedure", or that they "stay advisory in the model", sends
        them to check `.passed` by eye on a field that already decided it. That is worse than a
        stale sentence: it is a sentence that instructs.
        """
        text = _normalized(PLUGIN_ROOT / surface)
        revived = [(claim, why) for claim, why in self._RETIRED_CLAIMS if claim in text]
        assert not revived, "\n  ".join(
            [f"{surface} revives a retired claim:", *(f"{claim!r} — but {why}" for claim, why in revived)]
        )

    def test_the_retired_claim_sensor_would_fire(self, tmp_path: Path):
        """Anti-vacuity, through the REAL reader: a fragment that cannot match guards nothing.

        Fed HARD-WRAPPED, because that is how these documents are written and a raw substring
        check would pass on exactly the text it exists to catch — the reason every sensor here
        reads its surface through `_normalized`.
        """
        for claim, _why in self._RETIRED_CLAIMS:
            wrapped = "prose that says\nthe " + claim.replace(" ", "\n") + "\nand should fail"
            surface = tmp_path / "surface.md"
            surface.write_text(wrapped, encoding="utf-8")
            assert claim in _normalized(surface), claim

    def test_optimize_method_quotes_no_tolerance_numbers(self):
        # The guardrails are bootstrap-derived; the ONE tolerance constant lives in the module.
        # A figure hand-copied into the prose is a second declaration of it — the same shape as the
        # pricing.py <-> pricing.ts mirror this repo had to add a parity test to defend — and it
        # goes stale silently, because nothing about changing a constant makes anyone reread a
        # markdown file. Derived from the constant, so this cannot itself go stale.
        from coder_eval.optimize.gate import MATERIALITY_FLOOR

        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        forbidden = {f"{MATERIALITY_FLOOR:.0%}", f"{MATERIALITY_FLOOR:g}", f"{MATERIALITY_FLOOR:.2f}"}
        present = sorted(f for f in forbidden if f in method)
        assert not present, (
            f"reference/optimize-method.md quotes {present} — the rendered value of MATERIALITY_FLOOR. "
            "The guardrail tolerance is owned by coder_eval.optimize.gate; describe the guardrail as "
            "bootstrap-derived and carry no figure, or the prose drifts the moment the constant moves."
        )

    def test_optimize_method_states_a_stage_c_criterion_for_each_track(self):
        """Stage C's acceptance criterion is a DIFFERENT quantity per track, and it said only one.

        The shipped § Stage C text was activation-only — "F1 remains the promotion metric … require
        the F1 direction to reproduce" — which instructs an execution reader to check a number their
        track never produces. On that track the paired `weighted_score` block IS the result, not a
        corroboration of one.

        Asserted by TOKEN rather than by sentence because the prose is pitched at a reader and will be
        reworded; what may not vanish is that both metrics are named as the criterion, and that the
        computed outcomes exist. `f1` alone would be vacuous — the section already mentions F1 nine
        times — so the anchor is the per-track heading plus each track's metric plus the four
        outcomes, which are the model's own `Literal` values and are derived from it here rather than
        retyped.
        """
        import typing

        from coder_eval.models import ConfirmVerdict

        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        stage_c = method[method.index("### Stage C") :]
        assert "acceptance criterion, per track" in stage_c, (
            "reference/optimize-method.md § Stage C no longer states its criterion PER TRACK. "
            "Activation promotes on f1.yes and execution on per-row weighted_score, so one "
            "criterion tells one of the two readers to check a quantity their track never produces."
        )
        for metric in ("f1.yes", "weighted_score"):
            assert metric in stage_c, f"§ Stage C does not name {metric} as an acceptance criterion"
        outcomes = typing.get_args(ConfirmVerdict.model_fields["outcome"].annotation)
        assert len(outcomes) == 4, f"ConfirmVerdict.outcome no longer has four values: {outcomes}"
        for outcome in outcomes:
            assert outcome.upper() in stage_c, (
                f"§ Stage C does not name the {outcome!r} outcome, so a reader cannot tell what the "
                "computed verdict can say"
            )

    def test_the_proposal_prompt_names_scripts_as_edit_targets(self):
        """The gap that produced this: three surfaces, zero mentions, and nobody noticed.

        A skill is a DIRECTORY, and the highest-leverage candidate class — replacing six prose steps
        with a bundled script — was nowhere described as legal. Without a sensor this paragraph is one
        careless edit from vanishing, and its absence is silent: nothing fails, the proposer simply
        stops being told, and the class of candidate stops being written.

        Asserted by TOKEN rather than by sentence, because prose pitched at a proposer will be
        reworded. `scripts/` alone would be too weak — the file mentions `scripts/` in the leak
        paragraph — so the anchor is the edit-target CLAIM plus the hypothesis it names.
        """
        prompt = _normalized(PLUGIN_ROOT / "reference" / "proposal-prompt.md")
        assert "legitimate edit targets" in prompt, (
            "reference/proposal-prompt.md no longer names scripts and reference files as edit "
            "targets. A skill is a directory, and a candidate that moves instruction into a script "
            "is the highest-leverage shape there is — a proposer not told so will not write one."
        )
        for token in ("scripts/", "reference files", "determinism"):
            assert token in prompt, f"the scripts-as-edit-targets paragraph no longer names {token!r}"
        # The three constraints that follow from it, each of which is silent when dropped.
        assert "allowed_tools" in prompt, "the paragraph must say a bundled script needs Bash in allowed_tools"
        assert "skill_text" in prompt, "it must say the leak check reads the whole directory"
        assert "Activation is untouched" in prompt, (
            "it must say a scripts-only candidate cannot move the activation number — a reader will "
            "otherwise look for an effect that cannot be there"
        )

    def test_the_proposal_prompt_supplies_the_passing_rows(self):
        # A proposer shown only failures optimizes for them alone, and an edit that fixes three rows
        # while breaking two is a net loss the aggregate hides. The caveats are part of the claim:
        # the evidence is genuinely mixed, so a sensor that let the honest hedge be deleted would be
        # pinning an overclaim.
        prompt = _normalized(PLUGIN_ROOT / "reference" / "proposal-prompt.md")
        assert "must not regress" in prompt, (
            "reference/proposal-prompt.md no longer supplies the PASSING rows. Only the failing rows "
            "were ever handed over, so nothing told the proposer what it was trading against."
        )
        assert "sample, not the whole passing set" in prompt, "the sampling caveat must stay"
        assert "genuinely mixed" in prompt, (
            "the honest caveat must stay: whether failures-plus-successes beats failures-only flips "
            "across settings, and a sensor pinning the claim without it pins an overclaim"
        )
        assert "regression_check" in prompt, (
            "the proposer must be pointed at the regression corpus BEFORE it writes — the skill only "
            "reads it at Step 10, after the round is paid for"
        )

    def test_the_stop_rule_counts_candidates_not_only_rounds(self):
        """The patience is a budget in HYPOTHESES, and rounds are a poor proxy for them.

        Two rounds of four candidates have tested eight; two rounds of one have tested two and say
        almost nothing. A reader deciding whether a skill is at its ceiling needs the count that
        actually failed.
        """
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        stop_rule = skill[skill.index("## Step 13") :]
        assert "candidates gated across" in stop_rule, (
            "optimize-skill's Step 13 no longer prints a cumulative CANDIDATE count beside the round "
            "count, so 'two rounds promoted nothing' cannot be told from two rounds of one candidate"
        )
        # `_normalized` collapses the file's hard wrapping, so the phrase is matched unwrapped.
        assert "budget in candidates" in stop_rule, (
            "Step 13 must say the patience is a budget in candidates rather than in rounds"
        )

    def test_the_skill_states_the_leak_scans_verbatim_boundary(self):
        # The other half of the `COST_FRONT_ADVISORY` precedent the constant's own comment cites: a
        # shared constant stops the RENDERED block drifting, and this stops the PROSE beside it
        # drifting. Without both, "the claim cannot exist in two files at two vintages" is a wish.
        #
        # A clean scan is the moment a reader is most likely to over-read it, so the skill must say
        # what clean does not prove — in its own words, hence an anchor rather than the sentence.
        from coder_eval.reports_optimize import LEAK_SCAN_BOUNDARY

        anchor = "verbatim"
        assert anchor in LEAK_SCAN_BOUNDARY.lower(), "the boundary constant no longer contains its own anchor"
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        assert anchor in skill.lower(), (
            "optimize-skill's SKILL.md no longer says the leak scan catches the VERBATIM form only. "
            "A clean result is not a proof of generalization, and prose that loses that turns a "
            "preflight into a licence."
        )

    def test_optimize_surfaces_call_the_cost_front_advisory(self):
        # Derived from the constant exactly as the MATERIALITY_FLOOR sensor is, so the claim cannot
        # exist in three files at three vintages. Asserted PER SURFACE rather than against the
        # concatenation: both carry it today, so a pair-wide check would stay green if either
        # section were deleted whole — and the procedure half is what an agent acts on.
        from coder_eval.reports_optimize import COST_FRONT_ADVISORY

        # A distinctive substring of the constant rather than the whole sentence: the prose is
        # hard-wrapped and pitched at a reader, so it paraphrases around this anchor.
        anchor = "advisory"
        assert anchor in COST_FRONT_ADVISORY, "the advisory constant no longer contains its own anchor word"
        for name in ("reference/optimize-method.md", "skills/optimize-skill/SKILL.md"):
            surface = _normalized(PLUGIN_ROOT / name)
            assert anchor in surface, (
                f"{name} no longer describes the cost/quality front as {anchor!r}. It is a REPORTED "
                "view: promotion still requires the primary statistic to separate and every "
                "guardrail to hold, and prose that loses that reads as a licence to promote past "
                "the cost veto."
            )
            # A phrase unique to the NEW paragraphs. `"guardrail"` would have been vacuous —
            # it appears 7x in the method file and 12x in the skill already, so deleting the whole
            # cost-front section would have left the assertion green.
            assert "never a promotion" in surface, (
                f"{name} no longer distinguishes the cost VETO from the cost OBJECTIVE — without "
                "it a reader takes the front for a second gate and promotes past the guardrail"
            )

    def test_optimize_surfaces_quote_no_resample_count(self):
        # Same rule as the tolerance above, two constants along. GATE_RESAMPLES is DERIVED from
        # GATE_P_PRECISION / GATE_MAX_FAMILY / DEFAULT_ALPHA, so a figure typed into the prose is a
        # second declaration of a number the module computes — and the derivation is exactly the
        # kind of thing that gets retuned once and then disagrees with two markdown files forever.
        #
        # DEFAULT_ALPHA rides the same sensor rather than a third near-identical one. The sizing
        # table put it at risk: it needs the Holm threshold in a cell, and the honest way to write
        # that is `alpha/S` bound to the constant by the CE039 claim — not a retyped `0.05/5`.
        from coder_eval.optimize.gate import GATE_RESAMPLES
        from coder_eval.reports_stats import DEFAULT_ALPHA

        forbidden = {
            f"{GATE_RESAMPLES:d}": "GATE_RESAMPLES, which the module derives from two stated requirements",
            f"{GATE_RESAMPLES:,d}": "GATE_RESAMPLES, which the module derives from two stated requirements",
            f"{DEFAULT_ALPHA:g}": "DEFAULT_ALPHA, which reports_stats owns",
        }
        for name in ("reference/optimize-method.md", "skills/optimize-skill/SKILL.md"):
            surface = _normalized(PLUGIN_ROOT / name)
            present = sorted(f for f in forbidden if f in surface)
            assert not present, (
                f"{name} quotes {present} — the rendered value of "
                + "; ".join(forbidden[f] for f in present)
                + ". Write the symbol (`alpha`) and let the block or the CE039 claim bind it; the "
                + "rendered verdict reports the values it actually used."
            )

    def test_no_sensor_inlines_the_normalization_idiom(self):
        # `_normalized` exists because a hard wrap silently defeated a sensor and shipped a
        # stale skill count past 91 green tests. The extraction only helps if every sensor
        # actually uses it, and a new one is usually copied from a neighbour — so if the
        # neighbour inlines the idiom, the bug propagates. This forbids the raw form.
        #
        # Over the WHOLE split suite, not `__file__`: the sensors this polices used to share one
        # module and now sit across the package, so reading this file alone would police one of them
        # and report a clean bill of health for all the others. That narrowing is the standing
        # hazard of splitting a monolith, and it is silent by construction — which is why the count
        # below is derived from disk rather than written down.
        sources = [*sorted(pathlib.Path(__file__).parent.glob("*.py")), TESTS_ROOT / "test_custom_lint.py"]
        assert len(sources) > 10, "GAP: the split suite is not where this scan expects it"
        offenders = [
            f"{path.name}:{n}: {line.strip()}"
            for path in sources
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            # The one legitimate occurrence is `_normalized`'s own body, exempted by exact
            # match rather than by line number so the guard survives edits above it.
            if '" ".join(' in line
            and ".read_text(" in line
            and ".split()" in line
            and line.strip() != _NORMALIZED_IMPL
            # ...and skip this guard's own machinery, which must mention the pattern to test it.
            and "_NORMALIZED_IMPL" not in line
        ]
        assert not offenders, (
            "these lines inline the whitespace-normalization idiom instead of calling "
            f"`_normalized()`: {offenders}. A sensor copied from one of them inherits the "
            "wrapped-phrase blind spot that `_normalized` exists to close."
        )

    def test_count_sensor_catches_a_wrapped_phrase(self, tmp_path: Path):
        # The self-test for the fix above, driven through the REAL matcher rather than
        # through `_normalized` alone. That distinction is the whole value: asserting only
        # that `_normalized` collapses whitespace leaves the sensor free to be reverted to a
        # raw `read_text` with every test still green — a guard that guards nothing.
        #
        # `docs/PLUGIN.md` said "All six\n  skills read it" while seven shipped, and the
        # count sensor passed 91 lint tests: the hard wrap put a newline between the two
        # words the substring check needed adjacent.
        surface = tmp_path / "PLUGIN.md"
        surface.write_text("All six\n  skills read it, which is the point.\n", encoding="utf-8")

        assert "six skills" not in surface.read_text(encoding="utf-8"), (
            "this test's premise is gone: the wrapped form is now literally adjacent, so it "
            "no longer demonstrates what the normalization buys"
        )
        assert _wrong_skill_count_offenders({"wrapped": surface}, count=7, auto=4) == ["wrapped: 'six skills'"], (
            "the count sensor found nothing in a surface reading 'All six\\n  skills' while "
            "seven ship. It has stopped collapsing whitespace, so any wrapped count phrase "
            "slips past it — the exact bug that let the stale count ship green."
        )

        # The converse, so a passing sensor is not simply one that reports everything: the
        # correct count in the same wrapped shape must yield no finding.
        clean = tmp_path / "CLEAN.md"
        clean.write_text("All seven\n  skills read it, which is the point.\n", encoding="utf-8")
        assert _wrong_skill_count_offenders({"clean": clean}, count=7, auto=4) == []

    def test_optimize_skill_handoff_names_grader(self):
        # The handoff is the seam. optimize-skill refuses to author a suite (one written by the
        # thing that will judge it is fitted to it) and points at `task` — so it has to say what
        # comes back, or a user hands over an ungated instrument and pays for a stage on it.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        assert "outcome-suite mode" in skill, (
            "optimize-skill's handoff no longer names task's outcome-suite MODE, so the instruction "
            "'point at /coder-eval:task' does not say which branch of it to ask for"
        )
        assert "grader script" in skill and "expectations" in skill, (
            "the handoff no longer names the grader among what `task` produces"
        )
        assert "separation margin" in skill, (
            "the handoff no longer asks for the discrimination gate's margin — the number that says "
            "whether the instrument measures anything at all"
        )


@pytest.mark.lint
class TestCE066SkillImportsOnlyTheApi:
    """CE066 — the skill-facing surface is DECLARED, not derived from whatever the binder resolves.

    Three parts, the shape `TestCE026ActionDocSurfaces` sets: the real check, an anti-vacuity check
    that the reader reads something, and synthetic positives proving the check can fail.

    See `tests/lint/skill_api_imports.py` for what this does and does not pin — in particular that it
    says nothing about whether a composite is USED well, only about which module a fence reaches for.
    """

    SKILL = PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md"

    def test_the_real_skill_imports_only_the_declared_surface(self):
        from tests.lint.skill_api_imports import find_foreign_imports

        findings = find_foreign_imports(self.SKILL)
        assert not findings, (
            "\noptimize-skill's SKILL.md reaches past `coder_eval.optimize.api`. Every guard, "
            "fallback and track branch belongs in a composite where a test can reach it — a fence "
            "that still needs a primitive is a fence not finished:\n\n" + "\n".join(f"  {f}" for f in findings)
        )

    def test_the_reader_reads_the_real_fences(self):
        # Anti-vacuity: a reader that found no fences reports no offenders, which is byte-identical
        # to a clean file. The skill has fifteen and every one of them imports the surface.
        from tests.lint.skill_api_imports import DECLARED_SURFACE, coder_eval_imports, python_fences

        fences = python_fences(self.SKILL.read_text(encoding="utf-8"))
        assert len(fences) >= 10, f"the reader found only {len(fences)} python fences"
        importing = [f for f in fences if coder_eval_imports(f) == {DECLARED_SURFACE}]
        assert len(importing) == len(fences), (
            f"only {len(importing)} of {len(fences)} fences import the declared surface — every one "
            "should, since a fence that imports nothing from the library is not driving anything"
        )

    def test_a_foreign_import_is_reported_with_its_fence(self, tmp_path):
        from tests.lint.skill_api_imports import find_foreign_imports

        markdown = tmp_path / "s.md"
        markdown.write_text(
            "# skill\n\n```python\nfrom coder_eval.optimize.api import leak_report\n```\n\n"
            "```python\nfrom coder_eval.optimize.load import load_arm_rows\n```\n",
            encoding="utf-8",
        )

        findings = find_foreign_imports(markdown)
        assert len(findings) == 1, findings
        assert "coder_eval.optimize.load" in findings[0]
        assert "fence 2" in findings[0], "a fifteen-fence file needs the position, not just the module"

    def test_a_plain_import_form_is_caught_too(self, tmp_path):
        # `import coder_eval.optimize.load` is the same reach with different syntax.
        from tests.lint.skill_api_imports import find_foreign_imports

        markdown = tmp_path / "s.md"
        markdown.write_text("```python\nimport coder_eval.optimize.load\n```\n", encoding="utf-8")
        assert len(find_foreign_imports(markdown)) == 1

    @pytest.mark.parametrize(
        ("body", "why"),
        [
            pytest.param("import pathlib\nimport subprocess\n", "stdlib is not in scope", id="stdlib"),
            pytest.param("from coder_eval.optimize.api import leak_report\n", "the declared surface", id="declared"),
            pytest.param("x = 1  # coder_eval.optimize.load\n", "a comment is not an import", id="comment"),
            pytest.param("def f():\n    '''coder_eval.optimize.load'''\n", "nor a docstring", id="docstring"),
            pytest.param("this is not python(", "an unparseable fence is the binder's fault", id="broken"),
        ],
    )
    def test_what_it_deliberately_does_not_flag(self, tmp_path, body: str, why: str):
        from tests.lint.skill_api_imports import find_foreign_imports

        markdown = tmp_path / "s.md"
        markdown.write_text(f"```python\n{body}```\n", encoding="utf-8")
        assert find_foreign_imports(markdown) == [], why

    def test_a_module_path_in_prose_outside_any_fence_is_not_an_import(self, tmp_path):
        # The CE039 lesson: a substring scan over a heavily-prosed file reports the documentation.
        from tests.lint.skill_api_imports import find_foreign_imports

        markdown = tmp_path / "s.md"
        markdown.write_text(
            "Under the block, `coder_eval.optimize.load` supplies the rows.\n\n"
            "```bash\npython -c 'import coder_eval.optimize.load'\n```\n",
            encoding="utf-8",
        )
        assert find_foreign_imports(markdown) == []
