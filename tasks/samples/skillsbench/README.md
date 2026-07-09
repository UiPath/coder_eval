# Sample tasks — SkillsBench

Runnable example tasks that exercise coder_eval against **real, non-UiPath**
coding and reasoning problems, so a first-time user can see a genuine
evaluation (not just a smoke test) with one command.

They are vendored from [**benchflow-ai/skillsbench**](https://github.com/benchflow-ai/skillsbench)
(Apache-2.0), pinned at commit
[`95b3e60`](https://github.com/benchflow-ai/skillsbench/tree/95b3e60de7a075090e2f9137f32e0d24da1a289b).
Each sample keeps the upstream prompt, inputs, and verifier; only the container
base and the coder_eval task wiring were added.

## What they do

| Task | What it exercises | Upstream |
|------|-------------------|----------|
| [`3d-scan-calc`](./3d-scan-calc) | Binary parsing + 3D geometry — decode a binary STL, isolate the largest connected component, compute mass = volume × density | [tasks/3d-scan-calc](https://github.com/benchflow-ai/skillsbench/tree/95b3e60de7a075090e2f9137f32e0d24da1a289b/tasks/3d-scan-calc) |
| [`court-form-filling`](./court-form-filling) | Document automation — fill a California small-claims court PDF form (SC-100) from a plain-text case description | [tasks/court-form-filling](https://github.com/benchflow-ai/skillsbench/tree/95b3e60de7a075090e2f9137f32e0d24da1a289b/tasks/court-form-filling) |
| [`dialogue-parser`](./dialogue-parser) | Parsing / file-format work — turn a branching script into a validated JSON graph + a Graphviz `.dot` | [tasks/dialogue-parser](https://github.com/benchflow-ai/skillsbench/tree/95b3e60de7a075090e2f9137f32e0d24da1a289b/tasks/dialogue-parser) |

## How to run them

### 1. Set up

- **Install & configure coder_eval** — follow the [top-level README](../../../README.md#installation): `make install`, then `cp .env.example .env` and set `ANTHROPIC_API_KEY`.
- **Start a Docker engine** — these samples use the `docker` sandbox driver, so a container runtime must be running (on macOS, Docker Desktop or colima).
- **Build the base image** the samples rebase onto:

  ```bash
  make docker-image
  ```

### 2. Run

```bash
# (optional) validate the specs without executing anything
coder-eval plan tasks/samples/skillsbench/*/*.yaml

# run a single sample
coder-eval run tasks/samples/skillsbench/3d-scan-calc/3d-scan-calc.yaml

# run all three
coder-eval run tasks/samples/skillsbench/*/*.yaml
```

Each task builds its container, runs the agent (Claude Sonnet 4.6 by default) in
an isolated sandbox, then scores the result with the upstream verifier. Budget
roughly 1–3 minutes per task.

## Interpreting the results

As each task finishes, the console prints its outcome:

```
Success criteria: 1/1 passed, weighted score: 1.000
Task finished: status=SUCCESS score=1.000
```

The **score is the verifier's reward, from 0.0 to 1.0**:

- `3d-scan-calc` and `court-form-filling` are all-or-nothing — `1.0` means every check passed, `0.0` means at least one failed.
- `dialogue-parser` gives **partial credit** — the fraction of the verifier's checks that passed.

Full results are written to `runs/<timestamp>/` (also reachable as `runs/latest`):
a machine-readable `task.json` per task plus a `task.html` you can open in a
browser. Summarize a run with:

```bash
coder-eval report runs/latest
```

Typical scores with the default model (Claude Sonnet 4.6) are below. The agent
is non-deterministic, so exact numbers vary from run to run:

| Task | Typical score |
|------|---------------|
| `3d-scan-calc` | 1.00 |
| `court-form-filling` | 1.00 |
| `dialogue-parser` | ~0.83 |

## How it works (brief)

- **`environment/Dockerfile`** rebases the upstream task onto `coder-eval-agent` and bakes the inputs at the paths the prompt expects.
- **`pre_run`** seeds those inputs into the agent's working directory and links the output path, so the agent's writes are captured whether it uses an absolute or a relative path.
- At scoring time a **`run_command`** criterion runs the upstream `verifier/test.sh` verbatim and reports the reward it writes, via `score_from_stdout`.
- The optional upstream **skills** (hints) are omitted, so a score reflects the agent's own ability.

## Attribution & license

These tasks are derived from benchflow-ai/skillsbench, licensed under Apache-2.0.
The prompts, input data, and verifiers are the work of their original authors
(credited in each upstream task's `task.md`). See the pinned commit above for the
source of truth.
