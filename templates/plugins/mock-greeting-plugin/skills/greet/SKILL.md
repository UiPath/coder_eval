---
name: greet
description: Generate a personalised Python greeting script. The script should accept a name argument (or default to "World") and print a timestamped greeting message. Use when the user says "write a greeting script", "create a hello script", or "make a script that greets someone".
allowed-tools: Read, Write, Bash
user-invocable: true
---

# Greet Skill

Write a Python script that greets a user by name with a timestamp.

## Requirements

- Script file: `greet.py`
- Accepts an optional name via `sys.argv[1]` (defaults to `"World"`)
- Prints: `Hello, <name>! Today is <YYYY-MM-DD>.`
- Uses the `datetime` module for the date
- Appends one line to `greet.log` after every successful run, in exactly this format:
  `GREET: <name> on <YYYY-MM-DD>`
- Exits with code 0 on success

## Example Output

```
$ python greet.py Alice
Hello, Alice! Today is 2026-03-12.

$ python greet.py
Hello, World! Today is 2026-03-12.
```

## Implementation Pattern

See `references/greeting-patterns.md` for the recommended implementation approach.
