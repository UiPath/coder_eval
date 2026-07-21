"""Mock agent for integration testing with dynamic file operations.

This mock agent inspects the task's success criteria and dynamically creates
appropriate files, making it reusable across different test scenarios without
requiring scenario-specific implementations.
"""

from pathlib import Path

from coder_eval.agent import Agent, AgentState
from coder_eval.models import TaskDefinition, TurnRecord


class MockAgent(Agent):
    """Mock agent that dynamically creates files based on success criteria.

    This mock inspects the task's success criteria and creates appropriate
    files, making it reusable across different test scenarios without
    requiring scenario-specific implementations.

    Supports three scenarios:
    - "success": Creates all files required by success criteria
    - "failure": Doesn't create any files (task fails)
    - "partial": Creates files but with incorrect content
    """

    def __init__(self, task: TaskDefinition, scenario: str = "success"):
        """Initialize mock agent with task definition.

        Args:
            task: Complete task definition (includes success criteria)
            scenario: Behavior scenario ("success", "failure", "partial")
        """
        self.task = task
        self.scenario = scenario
        self.state = AgentState.WORKING
        self.working_directory: Path | None = None
        self._iteration = 0  # Track iteration count for TurnRecord

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        sandbox_managed: bool = False,
    ) -> None:
        """Initialize mock agent with working directory.

        Args:
            working_directory: Path to the sandbox working directory
            env_path_prepend: Ignored. Accepted only to match the Agent ABC signature.
            plugin_tools_dir: Ignored. Accepted only to match the Agent ABC signature.
            sandbox_managed: Ignored. Accepted only to match the Agent ABC signature.
        """
        self.working_directory = Path(working_directory)
        self.state = AgentState.WORKING

    async def stop(self) -> None:
        """Stop mock agent and mark as finished."""
        self.state = AgentState.FINISHED

    def get_state(self) -> AgentState:
        """Return current agent state.

        Returns:
            Current state of the agent
        """
        return self.state

    async def communicate(self, user_input: str, **kwargs) -> TurnRecord:
        """Simulate agent turn based on configured scenario.

        Args:
            user_input: Prompt from orchestrator

        Returns:
            TurnRecord with simulated agent response and file changes

        Raises:
            ValueError: If scenario is unknown
        """
        self._iteration += 1  # Increment iteration count

        if self.scenario == "success":
            return self._success_turn(user_input)
        elif self.scenario == "failure":
            return self._failure_turn(user_input)
        elif self.scenario == "partial":
            return self._partial_turn(user_input)
        else:
            raise ValueError(f"Unknown scenario: {self.scenario}")

    def _success_turn(self, user_input: str) -> TurnRecord:
        """Simulate successful task completion.

        Dynamically creates files based on success criteria to make
        the mock more reusable and reduce test coupling.

        Args:
            user_input: The prompt from orchestrator

        Returns:
            TurnRecord with successful completion
        """
        # Inspect success criteria and create appropriate files
        for criterion in self.task.success_criteria:
            if criterion.type == "file_exists":
                # Create the file the criterion expects
                file_path = self.working_directory / criterion.path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("Mock file content created by MockAgent")

            elif criterion.type == "file_contains":
                # Create file with content that matches includes
                file_path = self.working_directory / criterion.path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                # Join all required includes to ensure they're all present
                content = "\n".join(criterion.includes)
                file_path.write_text(content)

            # Can extend with other criterion types as needed
            # For now, file-based criteria cover most integration test needs

        self.state = AgentState.FINISHED

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output="I've successfully completed all required file operations based on the task criteria.",
        )

    def _failure_turn(self, user_input: str) -> TurnRecord:
        """Simulate failed task (doesn't create required files).

        Args:
            user_input: The prompt from orchestrator

        Returns:
            TurnRecord with failure message and no file changes
        """
        self.state = AgentState.FINISHED

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output="I was unable to complete the task due to constraints.",
        )

    def _partial_turn(self, user_input: str) -> TurnRecord:
        """Simulate partial completion (creates files but with wrong content).

        Args:
            user_input: The prompt from orchestrator

        Returns:
            TurnRecord with partial completion and files with wrong content
        """
        # Create files but with incorrect content that won't satisfy criteria
        for criterion in self.task.success_criteria:
            if criterion.type in ("file_exists", "file_contains"):
                file_path = self.working_directory / criterion.path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                # Write wrong content that won't match file_contains criteria
                file_path.write_text("wrong content that doesn't match requirements")

        self.state = AgentState.FINISHED

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output="I've created files but they may not meet all requirements.",
        )
