# Mock Greeting Plugin

A minimal test plugin for validating the `coder-eval autogen` command.

Contains two pure-Python skills with no external dependencies, making generated
tasks fast to evaluate without network access or package installation.

## Skills

| Skill | Command | Description |
|-------|---------|-------------|
| **greet** | `/mock-greeting-plugin:greet` | Generate a personalised greeting script |
| **farewell** | `/mock-greeting-plugin:farewell` | Generate a farewell script with a countdown |

## Purpose

This plugin exists solely as a test fixture. It is intentionally simple so that:
- `coder-eval autogen` can be tested end-to-end quickly
- Generated tasks run in under 10 seconds each
- No API keys or network access are required to evaluate the tasks
