"""Tests for the evaluator implementations."""

from unittest.mock import Mock

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    RunCommandCriterion,
    SandboxConfig,
)
from coder_eval.sandbox import Sandbox


def test_success_checker_file_exists():
    """Test file existence checking."""
    config = SandboxConfig(driver="tempdir", python=None)
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


def test_success_checker_populates_pass_threshold_on_results():
    """Every CriterionResult returned by SuccessChecker carries the criterion's pass_threshold."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_eval_threshold")

    try:
        sandbox.setup()
        checker = SuccessChecker(sandbox)

        # Custom pass_threshold — result must reflect it
        criterion = FileExistsCriterion(path="missing.txt", description="missing", pass_threshold=0.5)
        result = checker.check(criterion)
        assert result.pass_threshold == 0.5

        # Unsupported type hits the KeyError branch — still carries the threshold
        class _Fake:
            type = "unsupported_type"
            description = "fake"
            pass_threshold = 0.42

        result = checker._check_single(_Fake(), reference_code=None)  # type: ignore[arg-type]
        assert result.pass_threshold == 0.42
        assert result.score == 0.0

    finally:
        sandbox.cleanup()


def test_success_checker_file_contains():
    """Test file content checking."""
    config = SandboxConfig(driver="tempdir", python=None)
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
        # includes: 0/1=0.0, no excludes so score = includes_score only
        assert result.score == 0.0

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
    config = SandboxConfig(driver="tempdir", python=None)
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
    config = SandboxConfig(driver="tempdir", python=None)
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
        RunCommandCriterion(command="echo test", expected_stdout="test", description="Test stdout match"),
        FileMatchesRegexCriterion(path="app.py", pattern="test", description="Test regex"),
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
    bad_criterion.pass_threshold = 0.9

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
    # includes: 1/2=0.5, no excludes so score = includes_score only
    assert result.score == 0.5
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


def test_success_checker_logs_carry_task_id_in_context(tmp_path):
    """SuccessChecker logs carry task_id when run inside task_log_handler."""
    from coder_eval.logging_config import setup_logging, task_log_handler

    setup_logging(level="INFO")
    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True
    checker = SuccessChecker(mock_sandbox)
    log_file = tmp_path / "task.log"

    with task_log_handler(log_file, task_id="my-test-task"):
        checker.check(FileExistsCriterion(path="test.txt", description="Test file"))

    assert "file_exists" in log_file.read_text()


def test_success_checker_works_without_task_context():
    """SuccessChecker works normally outside any task_log_handler context."""
    mock_sandbox = Mock()
    mock_sandbox.file_exists.return_value = True
    checker = SuccessChecker(mock_sandbox)
    result = checker.check(FileExistsCriterion(path="test.txt", description="Test file"))
    assert result.score == 1.0
