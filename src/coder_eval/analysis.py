"""Analysis tools for evaluation results.

This module provides utilities for analyzing and aggregating evaluation data,
particularly command telemetry statistics.

Separation of Concerns:
- Orchestrator: Coordinates evaluation execution
- Analysis: Processes results into insights
- Reports: Formats insights for human consumption
"""

from collections import Counter

from .models import CommandStatistics, CommandTelemetry, SlowestCommandInfo, TurnRecord


def calculate_command_statistics(turns: list[TurnRecord]) -> CommandStatistics:
    """Aggregate command telemetry across all turns.

    Args:
        turns: List of turn records from an evaluation

    Returns:
        CommandStatistics with aggregated metrics

    Example:
        >>> stats = calculate_command_statistics(evaluation.turns)
        >>> print(f"Agent used {stats.commands_by_tool['Read']} Read commands")
    """
    # Collect all commands from all turns
    all_commands: list[CommandTelemetry] = []
    for turn in turns:
        all_commands.extend(turn.commands)

    if not all_commands:
        return CommandStatistics(total_commands=0)

    # Count by tool type
    commands_by_tool: dict[str, int] = {}
    for cmd in all_commands:
        commands_by_tool[cmd.tool_name] = commands_by_tool.get(cmd.tool_name, 0) + 1

    # Timing statistics (only for commands with duration data)
    # Use `is not None` to include valid 0.0ms durations
    total_time = sum(cmd.duration_ms for cmd in all_commands if cmd.duration_ms is not None)
    timed_count = sum(1 for cmd in all_commands if cmd.duration_ms is not None)
    avg_time = total_time / timed_count if timed_count > 0 else 0

    # Find slowest commands (type-safe using SlowestCommandInfo model)
    commands_with_timing = [c for c in all_commands if c.duration_ms is not None]
    slowest = sorted(commands_with_timing, key=lambda x: x.duration_ms or 0, reverse=True)[:5]

    slowest_info = [
        SlowestCommandInfo(
            tool=cmd.tool_name,
            duration_ms=cmd.duration_ms if cmd.duration_ms is not None else 0.0,
            parameters=cmd.parameters,
            tool_id=cmd.tool_id,
        )
        for cmd in slowest
    ]

    # Success/failure/unknown rates
    successful = sum(1 for c in all_commands if c.result_status == "success")
    failed = sum(1 for c in all_commands if c.result_status == "error")
    unknown = sum(1 for c in all_commands if c.result_status == "unknown" or c.result_status is None)

    # Calculate success rate excluding unknown (avoid division by zero)
    known_commands = successful + failed
    success_rate = (successful / known_commands * 100) if known_commands > 0 else 0.0

    # Most common command sequence (3-grams)
    sequences = []
    for turn in turns:
        cmds = [c.tool_name for c in turn.commands]
        # Build 3-command sequences
        for i in range(len(cmds) - 2):
            sequences.append(f"{cmds[i]} → {cmds[i + 1]} → {cmds[i + 2]}")

    # Find most frequent sequence (handle edge case of no sequences)
    most_common = None
    if sequences:
        counter = Counter(sequences)
        most_common = counter.most_common(1)[0][0]

    return CommandStatistics(
        total_commands=len(all_commands),
        commands_by_tool=commands_by_tool,
        total_command_time_ms=total_time,
        avg_command_time_ms=avg_time,
        slowest_commands=slowest_info,
        successful_commands=successful,
        failed_commands=failed,
        unknown_commands=unknown,
        success_rate=success_rate,
        most_common_sequence=most_common,
    )
