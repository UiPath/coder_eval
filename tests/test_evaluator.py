"""Tests for the evaluator implementations."""

import json
from unittest.mock import Mock

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.evaluation.reviewer import LLMReviewer
from coder_eval.models import (
    CodeLintsCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    LLMReviewerConfig,
    ProgramStdoutEqualsCriterion,
    PytestCriterion,
    RunCommandCriterion,
    SandboxConfig,
)
from coder_eval.sandbox import Sandbox


def test_success_checker_file_exists():
    """Test file existence checking."""
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config, task_id="test_eval")

    try:
        sandbox_dir = sandbox.setup()

        # Create a test file
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("Hello")

        # Check file that exists - use SuccessChecker.check()
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(path="test.txt", description="Test file should exist")
        result = checker.check(criterion)
        assert result.score == 1.0

        # Check file that doesn't exist
        criterion = FileExistsCriterion(path="missing.txt", description="Missing file")
        result = checker.check(criterion)
        assert result.score == 0.0

    finally:
        sandbox.cleanup()


def test_success_checker_file_contains():
    """Test file content checking."""
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config, task_id="test_eval_contains")

    try:
        sandbox_dir = sandbox.setup()

        # Create a test file with content
        test_file = sandbox_dir / "app.py"
        test_file.write_text("import datetime\nprint('Hello, Claude!')")

        # Check file contains required strings - use SuccessChecker.check()
        checker = SuccessChecker(sandbox)
        criterion = FileContainsCriterion(
            path="app.py", includes=["Hello, Claude!", "datetime"], description="File should contain required strings"
        )
        result = checker.check(criterion)
        assert result.score == 1.0

        # Check file is missing required strings
        criterion = FileContainsCriterion(
            path="app.py", includes=["missing_string"], description="File should contain missing string"
        )
        result = checker.check(criterion)
        # includes: 0/1=0.0, excludes: 1.0 (no excludes), avg=(0.0+1.0)/2=0.5
        assert result.score == 0.5

        # Check file contains excluded strings
        criterion = FileContainsCriterion(
            path="app.py",
            includes=["Hello"],
            excludes=["datetime"],  # This IS in the file
            description="File should not contain datetime",
        )
        result = checker.check(criterion)
        # includes: 1/1=1.0, excludes: 0/1=0.0, avg=(1.0+0.0)/2=0.5
        assert result.score == 0.5

    finally:
        sandbox.cleanup()


def test_success_checker_run_command():
    """Test command execution checking."""
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config, task_id="test_eval_cmd")

    try:
        sandbox.setup()

        # Test successful command - use SuccessChecker.check()
        checker = SuccessChecker(sandbox)
        criterion = RunCommandCriterion(
            command="echo 'Hello'", timeout=5, expected_exit_code=0, description="Echo should succeed"
        )
        result = checker.check(criterion)
        assert result.score == 1.0
        assert "Hello" in result.details

        # Test failing command
        criterion = RunCommandCriterion(
            command="exit 1", timeout=5, expected_exit_code=0, description="This should fail"
        )
        result = checker.check(criterion)
        assert result.score < 1.0

    finally:
        sandbox.cleanup()


def test_success_checker_check_all():
    """Test checking multiple criteria."""
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config, task_id="test_eval_all")

    try:
        sandbox_dir = sandbox.setup()

        # Create test files
        (sandbox_dir / "file1.txt").write_text("content1")
        (sandbox_dir / "file2.txt").write_text("content2")

        checker = SuccessChecker(sandbox)

        criteria = [
            FileExistsCriterion(path="file1.txt", description="File 1 exists"),
            FileExistsCriterion(path="file2.txt", description="File 2 exists"),
            FileExistsCriterion(path="missing.txt", description="Missing file"),
        ]

        results = checker.check_all(criteria)

        assert len(results) == 3
        assert results[0].score == 1.0
        assert results[1].score == 1.0
        assert results[2].score == 0.0

    finally:
        sandbox.cleanup()


def test_llm_reviewer_parse_response():
    """Test parsing LLM responses with new field names."""
    config = LLMReviewerConfig(enabled=True, model="gpt-5-2025-08-07")

    reviewer = LLMReviewer(config)

    # Test valid JSON response with new field names
    valid_response = """
    Here is the code review:
    {
        "issues": "Script works but overcomplicated. Remove manual auth.",
        "score": 0.8,
        "next_steps": ["Add error handling", "Write tests"],
        "should_continue": true
    }
    """

    decision = reviewer._parse_response(valid_response)
    assert decision is not None
    assert decision.score == 0.8
    assert decision.issues == "Script works but overcomplicated. Remove manual auth."
    assert len(decision.next_steps) == 2
    assert decision.should_continue is True

    # Test response with only JSON
    json_only = json.dumps({"issues": "Task complete", "score": 1.0, "next_steps": [], "should_continue": False})

    decision = reviewer._parse_response(json_only)
    assert decision is not None
    assert decision.score == 1.0
    assert decision.issues == "Task complete"
    assert decision.should_continue is False

    # Test invalid response
    invalid_response = "This is not JSON at all"
    decision = reviewer._parse_response(invalid_response)
    assert decision is None


def test_llm_reviewer_build_prompt():
    """Test prompt building includes terse code review instructions."""
    config = LLMReviewerConfig(enabled=True, model="gpt-5-2025-08-07", temperature=0.0)

    reviewer = LLMReviewer(config)

    prompt = reviewer._build_review_prompt(
        task_description="Create a hello world script",
        agent_output="Agent created app.py with print statement",
        current_iteration=1,
        max_iterations=3,
    )

    # Assert task context present
    assert "Create a hello world script" in prompt
    assert "Iteration 1/3" in prompt
    assert "JSON" in prompt

    # Assert new terse field names present
    assert "issues" in prompt
    assert "next_steps" in prompt
    assert "score" in prompt

    # Assert terse instructions present
    assert "code reviewer" in prompt.lower()
    assert "No praise, no fluff" in prompt or "direct" in prompt.lower()

    # Assert examples present
    assert "Examples of GOOD" in prompt
    assert "Examples of BAD" in prompt


def test_llm_reviewer_disabled():
    """Test that reviewer returns None when disabled."""
    config = LLMReviewerConfig(enabled=False, model="gpt-5-2025-08-07")

    reviewer = LLMReviewer(config)

    result = reviewer.review(
        task_description="Test task", agent_output="Some output", current_iteration=1, max_iterations=3
    )

    assert result is None


def test_success_checker_dispatch():
    """Test pattern matching dispatcher works for all criterion types."""
    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True
    mock_sandbox.get_file_content.return_value = "test content"
    mock_sandbox.run_command.return_value = (0, "output", "")

    checker = SuccessChecker(mock_sandbox)

    # Test each criterion type dispatches correctly
    criteria = [
        FileExistsCriterion(path="test.txt", description="Test file exists"),
        FileContainsCriterion(path="app.py", includes=["test"], description="Test file contains"),
        RunCommandCriterion(command="echo test", description="Test run command"),
        ProgramStdoutEqualsCriterion(command="echo test", expected_output="test", description="Test stdout"),
        PytestCriterion(path="tests/", description="Test pytest"),
        FileMatchesRegexCriterion(path="app.py", pattern="test", description="Test regex"),
        CodeLintsCriterion(linter="ruff check", description="Test linter"),
    ]

    for criterion in criteria:
        result = checker.check(criterion)
        # Verify each returns a CriterionResult (doesn't raise TypeError)
        assert hasattr(result, "score")
        assert hasattr(result, "criterion_type")
        assert result.criterion_type == criterion.type


def test_success_checker_unsupported_type():
    """Test error handling for unsupported criterion type."""
    mock_sandbox = Mock()
    checker = SuccessChecker(mock_sandbox)

    # Create mock criterion with invalid type
    bad_criterion = Mock()
    bad_criterion.type = "invalid_type_that_does_not_exist"
    bad_criterion.description = "Test criterion with unsupported type"

    # Should return failed result instead of raising
    result = checker.check(bad_criterion)
    assert result.score == 0.0
    assert "Unsupported criterion type" in result.error
    assert result.criterion_type == "invalid_type_that_does_not_exist"


def test_success_checker_with_mocked_sandbox():
    """Test that checker can be tested in isolation with mocked sandbox."""
    # This demonstrates the key benefit of the refactoring:
    # Easy testing with mocked dependencies

    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True

    checker = SuccessChecker(mock_sandbox)
    criterion = FileExistsCriterion(path="test.txt", description="Test file")

    result = checker.check(criterion)

    assert result.score == 1.0
    mock_sandbox.file_exists.assert_called_once_with("test.txt")
    assert "exists" in result.details


def test_success_checker_mocked_file_contains():
    """Test file contains logic with mocked sandbox."""
    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True
    mock_sandbox.get_file_content.return_value = "Hello World! This is a test."

    checker = SuccessChecker(mock_sandbox)

    # Test successful match
    criterion = FileContainsCriterion(path="test.txt", includes=["Hello", "World"], description="Should match")
    result = checker.check(criterion)
    assert result.score == 1.0

    # Test missing include
    criterion = FileContainsCriterion(path="test.txt", includes=["Hello", "Missing"], description="Should fail")
    result = checker.check(criterion)
    # includes: 1/2=0.5, excludes: 1.0 (no excludes), avg=(0.5+1.0)/2=0.75
    assert result.score == 0.75
    assert "Missing" in result.details or "1/2" in result.details


def test_success_checker_mocked_command_execution():
    """Test command execution with mocked sandbox."""
    mock_sandbox = Mock()
    mock_sandbox.run_command.return_value = (0, "Success output", "")

    checker = SuccessChecker(mock_sandbox)

    criterion = RunCommandCriterion(command="echo test", expected_exit_code=0, description="Test command")

    result = checker.check(criterion)

    assert result.score == 1.0
    mock_sandbox.run_command.assert_called_once_with("echo test", timeout=30)
    assert "Exit code: 0" in result.details


def test_handle_criterion_errors_decorator():
    """Test that the @handle_criterion_errors decorator handles exceptions correctly."""
    mock_sandbox = Mock()
    # Make file_exists raise an exception to test decorator error handling
    mock_sandbox.file_exists.side_effect = RuntimeError("Simulated sandbox error")

    checker = SuccessChecker(mock_sandbox)

    # Test that FileExistsCriterion check catches exception via decorator
    criterion = FileExistsCriterion(path="test.txt", description="Test file")
    result = checker.check(criterion)

    # Verify decorator caught exception and returned failed result
    assert result.score == 0.0
    assert result.error is not None
    assert "Simulated sandbox error" in result.error
    assert result.criterion_type == "file_exists"
    assert result.description == "Test file"


def test_handle_criterion_errors_decorator_command():
    """Test decorator error handling for command execution criterion."""
    mock_sandbox = Mock()
    # Make run_command raise a timeout exception
    mock_sandbox.run_command.side_effect = TimeoutError("Command timed out after 30s")

    checker = SuccessChecker(mock_sandbox)

    criterion = RunCommandCriterion(command="long_running_task", expected_exit_code=0, description="Timeout test")
    result = checker.check(criterion)

    # Verify decorator caught exception and returned failed result
    assert result.score == 0.0
    assert result.error is not None
    assert "Command timed out" in result.error
    assert result.criterion_type == "run_command"


def test_handle_criterion_errors_decorator_file_contains():
    """Test decorator error handling for file_contains criterion."""
    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True
    # Make get_file_content raise an exception
    mock_sandbox.get_file_content.side_effect = PermissionError("Access denied")

    checker = SuccessChecker(mock_sandbox)

    criterion = FileContainsCriterion(path="test.txt", includes=["test"], description="Permission error test")
    result = checker.check(criterion)

    # Verify decorator caught exception and returned failed result
    assert result.score == 0.0
    assert result.error is not None
    assert "Access denied" in result.error
    assert result.criterion_type == "file_contains"


def test_llm_reviewer_parse_old_format_with_aliases():
    """Test backward compatibility: old 'assessment'/'suggestions' fields still work via Pydantic aliases."""
    config = LLMReviewerConfig(enabled=True, model="gpt-5-2025-08-07")

    reviewer = LLMReviewer(config)

    # Old format with deprecated field names (v0.1.0)
    old_response = """
    {
        "assessment": "The agent has made progress...",
        "score": 0.8,
        "suggestions": ["Consider adding tests"],
        "should_continue": true
    }
    """

    decision = reviewer._parse_response(old_response)

    # Should still parse via Pydantic aliases
    assert decision is not None
    assert decision.issues == "The agent has made progress..."  # Read via 'assessment' alias
    assert decision.score == 0.8
    assert decision.next_steps == ["Consider adding tests"]  # Read via 'suggestions' alias
    assert decision.should_continue is True


def test_success_checker_logs_include_task_id(caplog):
    """Test that SuccessChecker logs include task_id context when provided.

    Regression test: checker logs previously lacked the [task-name] prefix,
    making it hard to trace which task a criterion result belonged to in
    concurrent runs.
    """
    import logging

    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True

    checker = SuccessChecker(mock_sandbox, task_id="my-test-task")

    # Verify logger is a LoggerAdapter with task_id
    assert isinstance(checker.logger, logging.LoggerAdapter)
    assert checker.logger.extra["task_id"] == "my-test-task"

    # Run a check and verify the task_id appears in log records
    criterion = FileExistsCriterion(path="test.txt", description="Test file")
    with caplog.at_level(logging.INFO, logger="coder_eval.evaluation.checker"):
        checker.check(criterion)

    assert any("my-test-task" in r.message or getattr(r, "task_id", None) == "my-test-task" for r in caplog.records)


def test_success_checker_logs_without_task_id():
    """Test that SuccessChecker works correctly when task_id is omitted."""
    import logging

    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True

    checker = SuccessChecker(mock_sandbox)

    # Without task_id, logger should be the plain module logger (not an adapter)
    assert not isinstance(checker.logger, logging.LoggerAdapter)

    # Should still function normally
    criterion = FileExistsCriterion(path="test.txt", description="Test file")
    result = checker.check(criterion)
    assert result.score == 1.0
