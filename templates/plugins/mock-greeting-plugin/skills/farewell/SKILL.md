---
name: farewell
description: Generate a Python farewell script with a configurable countdown. The script prints a goodbye message followed by a numbered countdown. Use when the user says "write a farewell script", "create a goodbye script", or "make a countdown script".
allowed-tools: Read, Write, Bash
user-invocable: true
---

# Farewell Skill

Write a Python script that prints a goodbye message followed by a countdown.

## Requirements

- Script file: `farewell.py`
- Accepts an optional name via `sys.argv[1]` (defaults to `"World"`)
- Accepts an optional countdown start via `sys.argv[2]` (defaults to `3`, must be a positive integer)
- Output format:
  1. `Goodbye, <name>!`
  2. Countdown lines: `3... 2... 1...` (on separate lines)
  3. Final line: `See you next time.`
- Writes `farewell_complete` (exactly, no newline) to `farewell.status` on success
- Exits with code 0 on success, code 1 if countdown argument is not a positive integer

## Example Output

```
$ python farewell.py Alice 3
Goodbye, Alice!
3...
2...
1...
See you next time.

$ python farewell.py
Goodbye, World!
3...
2...
1...
See you next time.

$ python farewell.py Bob 0
Error: countdown must be a positive integer
```

## Constraints

- Pure Python, no third-party packages
- The countdown and farewell logic should be in separate functions for testability
