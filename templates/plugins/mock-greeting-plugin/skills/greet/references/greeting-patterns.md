# Greeting Patterns

## Standard Pattern

```python
import sys
from datetime import date


def greet(name: str = "World") -> str:
    today = date.today().strftime("%Y-%m-%d")
    return f"Hello, {name}! Today is {today}."


def log_greet(name: str, today: str) -> None:
    with open("greet.log", "a") as f:
        f.write(f"GREET: {name} on {today}\n")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "World"
    today = date.today().strftime("%Y-%m-%d")
    print(greet(name))
    log_greet(name, today)
```

## Key Points

- Always import `date` from `datetime`, not `datetime` itself, for date-only formatting
- Use `strftime("%Y-%m-%d")` for ISO 8601 date format
- Guard with `if __name__ == "__main__":` so the module is importable
- The `greet()` function should be pure (no side effects) and return a string
- Log format is exactly `GREET: <name> on <YYYY-MM-DD>` — no deviations
