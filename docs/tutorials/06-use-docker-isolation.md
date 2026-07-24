---
description: >-
  Switch a Coder Eval task from the temp-dir sandbox to a fresh Docker container
  per task — strong host isolation, a pinned toolchain, and task-specific
  dependencies.
---

# Tutorial 06 — Running Tasks in Docker Isolation

By default a task runs in a temp-dir sandbox on your host. This tutorial switches
that to a fresh **Docker container per task** — strong host isolation and a
pinned, reproducible toolchain — then shows how to add task-specific
dependencies. ~10 minutes.

For the complete reference, see [Docker Isolation](../DOCKER_ISOLATION.md).

## Why

- **Isolation** — agent-generated code can't touch files or the network outside
  the sandbox.
- **Reproducibility** — the image bakes in Python 3.13, Node 22 LTS, the Claude
  CLI, `uv`, and the matching `coder_eval` version, so results don't drift with
  host upgrades.

Aggregation (scores, reports, evalboard) still happens on the host — each
container is a sealed "run one task → emit one `task.json`" worker.

## Prerequisites

- You've done [Tutorial 01](01-first-evaluation.md) (installed + ran a task).
- **Docker** installed and the daemon running — check with `docker info`. If that
  command errors, install Docker first (next section).

## Installing Docker

Pick your OS. After installing, verify with:

```bash
docker info            # daemon reachable? (no error = good)
docker run --rm hello-world   # pulls a tiny image and runs it
```

### Linux

Install **Docker Engine** (the daemon runs natively — no VM needed). The official
convenience script covers most distros (Ubuntu, Debian, Fedora, …):

```bash
curl -fsSL https://get.docker.com | sh
```

Or use your distro's packages directly — e.g. Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y docker.io
```

Then start the daemon and allow your user to run Docker without `sudo`:

```bash
sudo systemctl enable --now docker      # start now + on boot
sudo usermod -aG docker "$USER"         # add yourself to the docker group
newgrp docker                           # apply the group in this shell (or log out/in)
```

For the packaged Docker Engine (Docker CE with Compose/Buildx), follow the
distro-specific apt/dnf repo steps at
<https://docs.docker.com/engine/install/>.

### macOS

Install **Docker Desktop** (bundles the engine in a lightweight VM):

- Download the installer for your chip (Apple Silicon vs Intel) from
  <https://docs.docker.com/desktop/install/mac-install/>, **or** with Homebrew:

  ```bash
  brew install --cask docker
  ```

- Launch **Docker Desktop** once (from Applications) so it starts the daemon and
  installs the `docker` CLI. The whale icon in the menu bar means it's running.
  The `docker` commands then work from any terminal.

### Windows

Install **Docker Desktop** with the **WSL 2** backend, and run coder-eval from
inside a WSL 2 Linux distro (the framework targets Linux/macOS):

1. Enable WSL 2 and install a distro (e.g. Ubuntu):

   ```powershell
   wsl --install
   ```

2. Install Docker Desktop from
   <https://docs.docker.com/desktop/install/windows-install/>. During setup keep
   **"Use the WSL 2 based engine"** enabled, and under
   *Settings → Resources → WSL integration* enable your distro.
3. Open your **WSL 2 Ubuntu** terminal and run `docker info` there — you'll run
   all `coder-eval` commands from inside WSL, not PowerShell.

## 1. Build the framework image (one time)

```bash
make docker-image
```

This builds `coder-eval-agent:<version>` (also tagged `:latest`) with both
built-in agents (claude-code + Codex) and **needs no credentials**. `<version>`
is your installed `coder_eval` version (find it with `coder-eval --version`).
Rebuild only when you upgrade `coder_eval` or the pinned toolchain. It's all you
need for the common case (steps 2–5 below).

Related build targets:

| Target | Builds | When you need it |
| --- | --- | --- |
| `make docker-image` | `coder-eval-agent` (rebase base) | The default — the base image from which task Dockerfile should inherit `FROM coder-eval-agent` (step 5). |
| `make docker-images` | **both** `coder-eval-agent` **and** `coder-eval-runtime` | coder-eval-runtime is used when a task Dockerfile brings its own base image.|
| `make docker-image-full` | `coder-eval-agent` **+ the `uipath` extra** | Tasks that shell out to the `uipath` CLI. The `uipath` SDK resolves from public PyPI — no credentials needed. |

> `make docker-images` is just `make docker-image` + `make coder-eval-runtime`.
> Both base images are independent and persistent — build once, run any mix of
> rebase and inject tasks. See
> [Docker Isolation](../DOCKER_ISOLATION.md#tasks-that-bring-their-own-base-image-the-runtime-kit-coder-eval-runtime)
> for inject-mode details.

## 2. Run any task in a container

Add `--driver docker` to the `run` command from Tutorial 01:

```bash
uv run coder-eval run tasks/hello_date.yaml --driver docker
```

`--driver docker` is a thin alias for `-D sandbox.driver=docker`. Everything else
(streaming, reports, exit codes) works exactly as before.

## 3. Confirm it ran in Docker

The tell-tale sign is a **`docker.log`** in the task dir (the container's
stdout/stderr) — the tempdir driver never produces one:

```bash
find runs/latest -name docker.log
```

And if you `docker ps` *during* a run you'll see one `coder-eval-<task>-…`
container per in-flight task; they're removed (`--rm`) when the task finishes.
Scores and reports are still read the usual way (`coder-eval report runs/latest`)
— the driver doesn't change how results are aggregated.

## 4. Make a task always use Docker

Rather than passing the flag every time, set it in the task YAML:

```yaml
sandbox:
  driver: docker
  docker:
    network: bridge        # or "none" for a fully sealed run (no network)
    image: my-custom:tag # optional: override the default framework image
```

## 5. Add task-specific dependencies with a Dockerfile

When a task needs extra system packages or Python libs, ship a Dockerfile and
have coder-eval build it before the run:

```yaml
sandbox:
  driver: docker
  docker:
    dockerfile_path: ./environment/Dockerfile   # relative to the task YAML
```

```dockerfile
# environment/Dockerfile
FROM coder-eval-agent:latest        # REQUIRED — inherits the runtime + entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils
RUN pip install --no-cache-dir PyMuPDF==1.24.10
COPY input/ /root/input/            # build context = this Dockerfile's parent dir
```

- The built image is cached as `coder-eval-task-<task_id>:built`, so repeat runs
  reuse Docker's layer cache — edit the Dockerfile and only changed layers
  rebuild.
- `dockerfile_path` takes precedence over any `image:` value.

> **⚠️ The `FROM coder-eval-agent:<version>` line is mandatory.** The container
> runs the coder-eval orchestrator via the framework image's `ENTRYPOINT`; a bare
> base (e.g. `FROM ubuntu:24.04`) has no entrypoint and the run fails with
> `exec: "--output": executable file not found in $PATH`. coder-eval detects this
> after the build and aborts with a hint. (`:latest` and `:<version>` are both
> tagged by `make docker-image`, so either works in the `FROM`.) Tasks whose
> Dockerfile brings its own base — e.g. Fedora/`dnf` — use
> inject-mode instead; see
> [Docker Isolation](../DOCKER_ISOLATION.md#tasks-that-bring-their-own-base-image-the-runtime-kit-coder-eval-runtime).

## Next steps

- **Full Docker reference** (network modes, runtime kit, cleanup) →
  [Docker Isolation](../DOCKER_ISOLATION.md)
- **Task-file schema** → [Task Definition Guide](../TASK_DEFINITION_GUIDE.md)
- **Full CLI & config reference** → [User Guide](../USER_GUIDE.md)
