"""ADX table schema management."""

from . import adx

TABLES = ["SmokeRuns", "TaskResults", "CriteriaResults", "RunAnalysis"]

CREATE_SMOKE_RUNS = """\
.create table SmokeRuns (
    run_id: string,
    experiment_id: string,
    start_time: datetime,
    end_time: datetime,
    total_duration_seconds: real,
    tasks_run: int,
    tasks_succeeded: int,
    tasks_failed: int,
    tasks_error: int,
    success_rate: real,
    framework_version: string,
    git_commit: string,
    environment_info: dynamic
)
"""

CREATE_TASK_RESULTS = """\
.create table TaskResults (
    run_id: string,
    task_id: string,
    variant_id: string,
    task_description: string,
    status: string,
    weighted_score: real,
    duration_seconds: real,
    iteration_count: int,
    model_used: string,
    tags: dynamic,
    input_tokens: long,
    output_tokens: long,
    cache_creation_input_tokens: long,
    cache_read_input_tokens: long,
    total_tokens: long,
    total_cost_usd: real,
    turn_count: int,
    total_assistant_turns: int,
    total_commands: int,
    successful_commands: int,
    failed_commands: int,
    command_success_rate: real,
    commands_by_tool: dynamic,
    error_message: string,
    start_time: datetime,
    completed_at: datetime
)
"""

CREATE_CRITERIA_RESULTS = """\
.create table CriteriaResults (
    run_id: string,
    task_id: string,
    variant_id: string,
    criterion_type: string,
    description: string,
    score: real,
    details: string,
    error: string
)
"""

CREATE_RUN_ANALYSIS = """\
.create table RunAnalysis (
    run_id: string,
    experiment_id: string,
    generated_at: datetime,
    analysis_markdown: string
)
"""

CREATE_COMMANDS = {
    "SmokeRuns": CREATE_SMOKE_RUNS,
    "TaskResults": CREATE_TASK_RESULTS,
    "CriteriaResults": CREATE_CRITERIA_RESULTS,
    "RunAnalysis": CREATE_RUN_ANALYSIS,
}


def drop_tables(cluster_uri: str, database: str) -> None:
    client = adx.get_client(cluster_uri)
    for table in TABLES:
        print(f"  Dropping {table}...")
        client.execute_mgmt(database, f".drop table {table} ifexists")
    print("  All tables dropped.")


def create_tables(cluster_uri: str, database: str) -> None:
    client = adx.get_client(cluster_uri)
    for table in TABLES:
        print(f"  Creating {table}...")
        client.execute_mgmt(database, CREATE_COMMANDS[table])
    print("  All tables created.")
