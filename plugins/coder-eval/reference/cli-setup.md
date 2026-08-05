# Installing the `coder-eval` CLI

Read by every skill that shells out to the CLI. Installing the plugin does **not**
install it: a plugin ships skills and references, not packages, so the first thing a
CLI-driving skill does is confirm the binary exists.

## The check

```bash
coder-eval --version
```

Exit 0 means you are done — carry on with the skill.

## When it is missing

**Offer the install and ask. Never install unprompted.** This writes to the user's
machine outside the repository, which is not something to do on their behalf because
a skill happened to need it — the same reason the run-spending skills state the cost
and ask first.

Say what is missing and why the skill needs it, then offer both forms and let the
user pick:

```bash
uv tool install coder-eval    # preferred: isolated, on PATH, no venv to activate
pip install coder-eval        # if uv is unavailable, or inside an active venv
```

Prefer `uv tool install` when `uv` is on PATH: it puts a single isolated binary on
PATH, so the CLI keeps working regardless of which project virtualenv is active.
Reach for `pip install` when `uv` is absent, or when the user wants the CLI inside a
virtualenv they have already activated.

On approval, run the chosen command, then **re-run `coder-eval --version` to confirm
it worked** before continuing. A silent install failure is worse than no install:
the skill would carry on and fail later at a command the user cannot connect to this
step.

If the user declines, stop and say which step needed it. Do not carry on and fail at
the first invocation — that is the failure mode this check exists to prevent.

## If the install succeeds but the command still is not found

The binary is installed somewhere not on PATH. `uv tool install` prints the target
directory; report that path and the fact that it needs to be on PATH, rather than
retrying the install or falling back to a different installer. Re-running an install
that already succeeded will not fix a PATH problem.

## Version skew

The plugin's version tracks the CLI's, so a plugin much newer than an installed CLI
can reference options the CLI does not have. If a documented flag is rejected as
unknown, report the installed version alongside the error and suggest upgrading
(`uv tool upgrade coder-eval`, or `pip install --upgrade coder-eval`) rather than
working around the missing flag.
