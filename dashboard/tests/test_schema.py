"""Tests for schema module."""

from unittest.mock import MagicMock, patch

from dashboard.schema import (
    CREATE_COMMANDS,
    TABLES,
    create_tables,
    drop_tables,
)


def test_tables_list():
    """All expected tables are defined."""
    assert "SmokeRuns" in TABLES
    assert "TaskResults" in TABLES
    assert "CriteriaResults" in TABLES
    assert "RunAnalysis" in TABLES


def test_create_commands_match_tables():
    """Every table has a corresponding CREATE command."""
    assert set(CREATE_COMMANDS.keys()) == set(TABLES)


@patch("dashboard.schema.adx.get_client")
def test_drop_tables(mock_get_client):
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    drop_tables("https://cluster", "db")

    assert mock_client.execute_mgmt.call_count == len(TABLES)
    for call_args in mock_client.execute_mgmt.call_args_list:
        args = call_args[0]
        assert args[0] == "db"
        assert ".drop table" in args[1]
        assert "ifexists" in args[1]


@patch("dashboard.schema.adx.get_client")
def test_create_tables(mock_get_client):
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    create_tables("https://cluster", "db")

    assert mock_client.execute_mgmt.call_count == len(TABLES)
    for call_args in mock_client.execute_mgmt.call_args_list:
        args = call_args[0]
        assert args[0] == "db"
        assert ".create table" in args[1]


def test_create_smoke_runs_has_expected_columns():
    ddl = CREATE_COMMANDS["SmokeRuns"]
    for col in ["run_id", "success_rate", "total_duration_seconds", "environment_info"]:
        assert col in ddl


def test_create_task_results_has_expected_columns():
    ddl = CREATE_COMMANDS["TaskResults"]
    for col in ["task_id", "weighted_score", "total_cost_usd", "commands_by_tool"]:
        assert col in ddl
