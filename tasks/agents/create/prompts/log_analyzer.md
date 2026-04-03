Create a UiPath coded agent that analyzes application log entries.

- Input: log_text (string — multiline log entries, one per line), severity_filter (string, optional — "ERROR", "WARN", "INFO", "DEBUG")
- Output: total_lines (int), error_count (int), warning_count (int), filtered_entries (list of strings — log lines matching severity_filter, or all if no filter), unique_sources (list of strings — unique source/module names)
- Entry point function: analyze_logs
- Log format: `[TIMESTAMP] [SEVERITY] [SOURCE] message`
  - Example: `[2024-01-15 10:30:00] [ERROR] [auth-service] Login failed for user admin`
- Parse each line, extract severity and source from brackets
- Lines that don't match the format should be skipped (don't count them)
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
