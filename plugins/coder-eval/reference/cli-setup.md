# Installing the `coder-eval` CLI

Read by every skill that shells out to the CLI. Installing the plugin does **not**
install it: a plugin ships skills and references, not packages, so the first thing a
CLI-driving skill does is confirm the binary exists.

## The check

```bash
coder-eval --version
```

Exit 0 means you are done — carry on with the skill.

## Version pin

The check above tells you what is *installed*. In a repository that already uses
coder-eval that is only half the question: the other half is what the project *expects*,
and being helpful with the wrong binary there does real damage.

**So resolve the pin before running anything else.** Where an eval tree already exists,
search from the eval root (located per `${CLAUDE_PLUGIN_ROOT}/reference/repo-layout.md`)
outward toward the repository root; where there is none yet — a repository being set up
for the first time still may pin the CLI — search the repository itself. Collect **every**
pin you find rather than stopping at the first, so a disagreement is discoverable at all:

- a version file, e.g. `.coder-eval-version`;
- a `coder-eval==` or `coder-eval @` requirement in a `requirements*.txt`, in
  `pyproject.toml`, or in a lock file beside one;
- a pinned install line in a build file or a CI workflow.

If every pin you found names the same version, that is the pin. If they disagree, report
all of them and ask — do not pick one.

**Prefer a project-local binary** when the project ships one — a virtualenv under the
eval root — over whatever is on `PATH`. A repository that carries its own CLI has already
answered which one is correct.

**Report the binary and the version you resolved** before validating anything, so the
user can see which CLI produced whatever comes next.

**If the resolved version differs from the pin, stop and say so. Do not validate, and do
not upgrade.** A schema error from a mismatched CLI is indistinguishable from a real one,
so the verdict is unsound in both directions — errors reported that are not there, real
ones missed. And "fixing" what a newer CLI complains about would edit the repository's
tasks into a shape its own pinned CLI rejects. Name the pinned version, name the one you
found, and let the user decide.

Cases worth handling rather than guessing at:

- **No pin anywhere** — proceed with the binary on `PATH`, and say which version it is.
- **A pin you cannot read** (empty, or a format you do not recognize) — that is "a pin
  exists but could not be read", not "no pin". Ask.
- **Two pins that disagree** — a CI workflow and a requirements file, say. Report both
  and ask; do not pick one.
- **A binary that does not answer `--version`** — that is itself a version signal: it
  predates the flag. Report it as unresolvable, rather than concluding no CLI is
  installed.
- **The pin is satisfied, but by a different binary than the one you would have run** —
  the order is pin first, then which binary satisfies it. Say which one you settled on
  and why, so a later command in the same session uses the same one.

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
unknown, report the installed version alongside the error — then let the pin decide what
to suggest:

- **The project pins a version** → match the pin. Upgrading past it is a repo-breaking
  action, not a fix: the pinned CI job, the lock file and every other checkout keep the
  old CLI, so the repository ends up authored against one that does not exist there. Say
  the flag needs a newer coder-eval than this project pins, and leave both the decision
  and the pin to the user.
- **No pin** → suggesting an upgrade is the right call
  (`uv tool upgrade coder-eval`, or `pip install --upgrade coder-eval`).

Either way, do not work around the missing flag by inventing an equivalent.
