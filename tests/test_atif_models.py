"""Tests for the vendored ATIF models (coder_eval.harbor.atif_models).

Fixture compatibility guarantee: ``tests/fixtures/atif/known_good_trajectory.json``
was validated ONCE against the real harbor package (harbor==0.20.0) and then
frozen — CI never installs harbor. Reproducible re-validation procedure:

    python3 -m venv /tmp/harbor-check && /tmp/harbor-check/bin/pip install -q 'harbor==0.20.0'
    /tmp/harbor-check/bin/python -c "from harbor.models.trajectories.trajectory import Trajectory; \\
        import json; Trajectory.model_validate(json.load(open('tests/fixtures/atif/known_good_trajectory.json'))); \\
        print('OK')"
    rm -rf /tmp/harbor-check

Last validated: harbor 0.20.0 (2026-07-20). If the vendored models and this
fixture ever disagree with harbor, re-run the procedure and reconcile.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.harbor import (
    AtifAgent,
    ContentPart,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)


FIXTURE = Path(__file__).parent / "fixtures" / "atif" / "known_good_trajectory.json"


def _agent() -> AtifAgent:
    return AtifAgent(name="claude-code", version="1.0.0")


def _step(step_id: int, source: str = "agent", **kwargs) -> Step:
    return Step(step_id=step_id, source=source, message=f"step {step_id}", **kwargs)


def _tool_step(step_id: int, tool_call_id: str = "toolu_01", source_call_id: str | None = "toolu_01") -> Step:
    return Step(
        step_id=step_id,
        source="agent",
        message="calling a tool",
        tool_calls=[ToolCall(tool_call_id=tool_call_id, function_name="Bash", arguments={"command": "ls"})],
        observation=Observation(results=[ObservationResult(source_call_id=source_call_id, content="ok")]),
    )


class TestHappyPath:
    def test_two_step_trajectory_with_tool_call_round_trips(self):
        t = Trajectory(agent=_agent(), steps=[_step(1, source="user"), _tool_step(2)])
        dumped = t.model_dump(exclude_none=True)
        # exclude_none is how harbor serializes (Trajectory.to_json_dict).
        reparsed = Trajectory.model_validate(dumped)
        assert reparsed == t

    def test_defaults(self):
        t = Trajectory(agent=_agent(), steps=[_step(1)])
        assert t.schema_version == "ATIF-v1.7"
        assert t.trajectory_id is None
        assert t.final_metrics is None


class TestValidators:
    def test_empty_steps_rejected(self):
        with pytest.raises(ValidationError, match="at least 1"):
            Trajectory(agent=_agent(), steps=[])

    def test_non_sequential_step_ids_rejected(self):
        with pytest.raises(ValidationError, match=r"expected 2 \(sequential from 1\), got 3"):
            Trajectory(agent=_agent(), steps=[_step(1), _step(3)])

    def test_step_ids_must_start_at_one(self):
        with pytest.raises(ValidationError, match=r"expected 1 \(sequential from 1\), got 2"):
            Trajectory(agent=_agent(), steps=[_step(2)])

    def test_cross_step_source_call_id_rejected(self):
        # Step 2's observation references step 1's tool_call_id — invalid.
        bad = _tool_step(2, tool_call_id="toolu_other", source_call_id="toolu_01")
        with pytest.raises(ValidationError, match="source_call_id 'toolu_01'"):
            Trajectory(agent=_agent(), steps=[_step(1, source="user"), bad])

    def test_null_source_call_id_allowed(self):
        step = _tool_step(1, source_call_id=None)
        Trajectory(agent=_agent(), steps=[step])  # does not raise

    def test_subagent_without_trajectory_id_rejected(self):
        sub = Trajectory(agent=_agent(), steps=[_step(1)])
        with pytest.raises(ValidationError, match="trajectory_id is required"):
            Trajectory(agent=_agent(), steps=[_step(1)], subagent_trajectories=[sub])

    def test_duplicate_subagent_trajectory_ids_rejected(self):
        sub1 = Trajectory(agent=_agent(), steps=[_step(1)], trajectory_id="dup")
        sub2 = Trajectory(agent=_agent(), steps=[_step(1)], trajectory_id="dup")
        with pytest.raises(ValidationError, match="not unique"):
            Trajectory(agent=_agent(), steps=[_step(1)], subagent_trajectories=[sub1, sub2])

    def test_agent_version_required(self):
        # Verified against harbor 0.20.0: Agent.version is REQUIRED, not optional.
        with pytest.raises(ValidationError, match="version"):
            AtifAgent(name="claude-code")  # type: ignore[call-arg]

    def test_content_part_text_requires_text(self):
        with pytest.raises(ValidationError, match="'text' field is required"):
            ContentPart(type="text")

    def test_content_part_text_forbids_source(self):
        with pytest.raises(ValidationError, match="'source' field is not allowed"):
            ContentPart(type="text", text="hi", source={"media_type": "image/png"})


class TestVersionTolerance:
    @pytest.mark.parametrize("version", ["ATIF-v1.0", "ATIF-v1.7", "ATIF-v1.9", "ATIF-v1.42"])
    def test_any_v1_minor_accepted(self, version):
        t = Trajectory(schema_version=version, agent=_agent(), steps=[_step(1)])
        assert t.schema_version == version

    @pytest.mark.parametrize("version", ["ATIF-v2.0", "ATIF-2.0", "v1.7", "garbage", ""])
    def test_non_v1_rejected(self, version):
        with pytest.raises(ValidationError, match="schema_version"):
            Trajectory(schema_version=version, agent=_agent(), steps=[_step(1)])


class TestFrozenFixture:
    def test_known_good_fixture_parses(self):
        """The harbor-0.20.0-validated fixture must parse with the vendored models."""
        t = Trajectory.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
        assert t.schema_version == "ATIF-v1.7"
        assert len(t.steps) == 3
        assert t.subagent_trajectories is not None
        assert t.subagent_trajectories[0].trajectory_id == "toolu_02"
        # Multimodal message variant (list of ContentPart) survives.
        assert isinstance(t.steps[2].message, list)
        # Sub-agent ref resolves against the embedded array.
        ref = t.steps[2].observation.results[0].subagent_trajectory_ref
        assert ref is not None and ref[0].trajectory_id == "toolu_02"

    def test_fixture_round_trips_through_vendored_models(self):
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        t = Trajectory.model_validate(raw)
        assert Trajectory.model_validate(t.model_dump(exclude_none=True)) == t


class TestExtraForbid:
    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            Trajectory(agent=_agent(), steps=[_step(1)], not_a_field=1)  # type: ignore[call-arg]

    def test_unknown_step_field_rejected(self):
        with pytest.raises(ValidationError):
            Step(step_id=1, source="agent", message="x", bogus=True)  # type: ignore[call-arg]

    def test_subagent_ref_requires_trajectory_id(self):
        with pytest.raises(ValidationError):
            SubagentTrajectoryRef()  # type: ignore[call-arg]
