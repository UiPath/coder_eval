---
description: >-
  Run each Coder Eval task in its own fresh Docker container — strong host
  isolation, a pinned reproducible agent runtime, and custom images for
  task-specific dependencies.
---

# Docker Isolation

Run each evaluation task inside its own fresh container. Strong host isolation and a pinned, reproducible agent runtime.

## When to use

Set `sandbox.driver: docker` on a task (or pass `--driver docker` on the CLI —
a thin alias for `-D sandbox.driver=docker`) when you want:

- **Isolation from the host filesystem/network** — agent-generated code can't reach files outside the sandbox.
- **A pinned toolchain** — the image bakes in Python 3.13, Node 22 LTS, `@anthropic-ai/claude-code`, the `pi` CLI (`@earendil-works/pi-coding-agent`), the codex/antigravity/litellm agent SDKs, `uv`, and the matching `coder_eval` version, so results don't drift with host upgrades.

Aggregation (P/R/F1, suite thresholds, reports) always stays on the host. Each container is a sealed "run one task → emit one `task.json`" worker.

## One-time setup

```bash
make docker-image        # core + both built-in agents (default; no credentials)
# opt in to the UiPath extra (resolves from public PyPI; no credentials):
make docker-image-full
```

Both build `coder-eval-agent:<pkg-version>` and tag it `:latest`.

- **`make docker-image`** installs the core package plus **both built-in agents** — claude-code (baked above) and Codex (`--extra codex`, public PyPI). It needs **no credentials** and covers the common case: claude-code or Codex tasks scored with `run_command` / `file_contains` (incl. converted skillsbench tasks). `llm_judge` / `agent_judge` work here too (they route through the run's Anthropic/Bedrock backend).
- **`make docker-image-full`** additionally installs the `uipath` extra. The `uipath` SDK resolves from **public PyPI** (per `uv.lock`), so the build needs **no credentials**. Use this only for tasks that shell out to the in-host `uipath` CLI. (Codex is already in the default image — no extra needed.)

> **Codex sandbox under Docker.** Codex's Landlock-backed `read-only` / `workspace-write` sandboxes can't initialize inside the eval container — their writes/execs fail silently and the agent produces no artifacts (a `score=0` FAILURE with no loud error). The docker runner sets `CODER_EVAL_IN_CONTAINER=1`, and the Codex agent honors it by falling back to `full-access`: the container itself is the trust boundary. Host runs (tempdir) are unaffected — Landlock works there and the marker is unset. So Codex tasks run under `--driver docker` with their natural `acceptEdits` permission mode; no need to set `bypassPermissions` by hand.

## Running a task in Docker

```bash
# Single task
coder-eval run path/to/task.yaml --driver docker

# All tasks
coder-eval run --driver docker

# Or in the task YAML
sandbox:
  driver: docker
  docker:
    network: bridge         # bridge | llm_only | none (see Network modes)
    image: my-custom:tag    # override the default image
```

## Network modes

`sandbox.docker.network` selects what the task container can reach. The same mode applies to
the grading container of a detached `coder-eval evaluate`.

| Mode | The container reaches | Use it for |
|---|---|---|
| `bridge` (default) | the full network | tasks that install packages or call other services |
| `llm_only` | only the model APIs and the hosts in `egress_allowlist`, through a proxy sidecar | agent tasks that must not use general internet |
| `none` | nothing | `agent: {type: none}` tasks only: a real agent cannot reach its model API |

```yaml
sandbox:
  driver: docker
  docker:
    network: llm_only
    egress_allowlist: [pypi.org, files.pythonhosted.org]   # optional, appended across layers
```

You can also set it per run: `-D sandbox.docker.network=llm_only` and
`-D 'sandbox.docker.egress_allowlist=["pypi.org"]'`.

### How the egress sidecar works

For each task the host creates:

1. A per-task `docker network create --internal --ipv6=false` network with
   `com.docker.network.bridge.inhibit_ipv4=true`. The network has no route out and no IPv4 gateway
   address on the host, so the task container cannot reach the internet or a host service.
2. An egress-proxy sidecar from the framework image `coder-eval-agent:<version>`. It joins the
   internal network (DNS alias `coder-eval-egress`) and the default `bridge`. It runs the host's
   own `egress_proxy.py`, bind-mounted read-only, as uid 65534 with all capabilities dropped and a
   read-only root file system. It opens TCP connections only to exact `host:port` targets.
3. The task container, on the internal network only, with `HTTPS_PROXY` / `HTTP_PROXY` (both
   cases) set to `http://coder-eval-egress:3128`, `NO_PROXY=localhost,127.0.0.1,::1`,
   `NODE_USE_ENV_PROXY=1` and `LITELLM_LOCAL_MODEL_COST_MAP=True`. A host value of a proxy
   variable (`*_PROXY`, `NO_PROXY`, `NODE_USE_ENV_PROXY`) is never forwarded. Its upstream DNS
   server is `192.0.2.1`, an address that is never routed: Docker's own DNS still resolves the
   sidecar, but an external name does not resolve. It also runs without `NET_RAW`, so it cannot
   send crafted packets onto the internal bridge.

Before the task container starts, the host sends one `CONNECT` per allowlisted target through
the sidecar. If one fails, the task is an `ERROR` row that names the failing targets. The host
removes the sidecar and the network on every path (success, error, Ctrl-C). If the host process
is killed, the sidecar stops by itself when the host heartbeat goes stale; see
[Troubleshooting egress](#troubleshooting-egress) to remove what is left.

A tool that ignores the proxy variables has no route, so it fails closed. It cannot bypass the
allowlist.

### The derived allowlist

You do not list the model APIs yourself. The host derives them from the parts of the task that
call a model, and adds nothing for the parts that do not:

| Source | Hosts added |
|---|---|
| API backend (`API_BACKEND`), only when a `claude-code` agent, an enabled `simulation:`, or an `llm_judge` / `agent_judge` criterion uses it | `direct`: `api.anthropic.com:443`, `platform.claude.com:443` (Claude Code OAuth refresh). `bedrock`: `bedrock-runtime.<AWS_REGION>.amazonaws.com:443`, `bedrock.<AWS_REGION>.amazonaws.com:443`. `litellm`: the host of the forwarded `LITELLM_BASE_URL` (a `localhost` value becomes `host.docker.internal`) |
| Agent | codex: the host of the forwarded `CODEX_BASE_URL`, else `api.openai.com:443`. antigravity: `generativelanguage.googleapis.com:443`. pi: the host of the `provider/` prefix of `agent.model` (`openrouter`, `anthropic`, `openai`, `google`; anything else or no model gives `openrouter.ai:443`). claude-code, opencode, delegate, none: nothing beyond the backend row |
| `system_one_judge` criteria | the host of each `base_url` (default `api.typesafe.ai:443`) |
| `egress_allowlist` | your entries |

A judge with its own `checker_context.api_route.route` uses the hosts of that backend. A
`route: litellm` judge configures its endpoint through `params`, so add that host to
`egress_allowlist`. Other forwarded `*_URL` variables, such as `UIPATH_URL`, add no host: a task
whose tools call that service lists the host in `egress_allowlist`. A Pi provider other than the
four above, and every OpenCode or Delegate provider, also needs its host in `egress_allowlist`.

### Extra egress hosts

`egress_allowlist` takes `host` or `host:port` entries; a bare host means port 443. A host is a DNS
name or an IPv4 address. Schemes, paths, wildcards and IPv6 addresses are refused when the task
loads. Entries are appended across the config layers, like `env_passthrough_extra`. Under `bridge`
or `none` the field is ignored.

Typical additions:

| Need | Entries |
|---|---|
| `sandbox.python.env_packages`, `pip`, `uv` | `pypi.org`, `files.pythonhosted.org` |
| `npm`, `npx -y` MCP servers | `registry.npmjs.org` |
| `git clone` over https from GitHub | `github.com` |
| `apt` (Debian) | `deb.debian.org:80` |
| `apt` (Ubuntu) | `archive.ubuntu.com:80`, `security.ubuntu.com:80` (amd64) or `ports.ubuntu.com:80` (arm64) |
| `apk` (Alpine) | `dl-cdn.alpinelinux.org` |
| A remote MCP server or a run-time plugin install | its host |
| `uv` downloading a managed Python not in the image | `github.com`, `objects.githubusercontent.com` (or bake the Python into the image) |

### Tool compatibility

The task image needs nothing new: its tools must honour the proxy variables.

| Tool | Under `llm_only` |
|---|---|
| curl, GNU wget, git over https, pip, uv, npm/npx, apt, apk | honour the proxy; a denied host fails fast (`CONNECT tunnel failed, response 403`, `npm error 403`; pip and uv retry for about 8–12 s first) |
| Python urllib / requests / httpx | honour the proxy (`trust_env` is on by default) |
| Python aiohttp | only with `trust_env=True` (litellm sets it); otherwise no route |
| Node `fetch` / `https` | only on Node ≥ 22.21 or ≥ 24.5, through `NODE_USE_ENV_PROXY=1`; older Node has no route. Node prints an `UNDICI-EHPA` warning on stderr |
| Go `net/http` default client | honours the proxy; a custom transport without `Proxy` has no route |
| busybox `wget` (Alpine) | plain http works; https always fails `400`, because it sends `GET https://…` instead of `CONNECT`. Use curl |
| dnf (Rocky, Fedora) | its metalink picks random mirrors, so an exact-host list cannot work. Pin a `baseurl` or bake the packages into the image |
| git over ssh, raw sockets, DNS lookups, Java without proxy flags | no route |

Every built-in harness in the framework image honours the proxy (Claude Code, Codex,
Antigravity, Pi). See [Run-Limit Parity § Network modes](agents/HARNESS_PARITY.md#network-modes-under-the-docker-driver).

The sidecar always runs the Debian framework image, so the task image's distribution does not
change the boundary. Debian, Ubuntu, Rocky Linux, Fedora and Alpine task images were tested as
clients. A runtime-kit image needs the framework image too (`make docker-images` builds both).

**Docker versions.** Docker 20.10 or later (`host-gateway` first shipped there). Docker 26.0,
25.0.5 or 23.0.11 or later is recommended: older daemons forward the DNS queries of an internal
network from the host (CVE-2024-29018). The black-hole upstream DNS server above stops that
forward, but a patched daemon removes the cause. Tested on Docker
Desktop 29 (macOS) and on Linux dockerd 20.10, 24 and 29, with both the `iptables` and the
`nftables` firewall backends. On Linux, `host.docker.internal` resolves to the `docker0` address,
so a LiteLLM proxy on the host must listen on `docker0` or `0.0.0.0`, as under `bridge`.

### Expected DENY lines

Some clients call hosts they do not need. These `DENY` lines are harmless:

| Client | Denied host |
|---|---|
| Claude Code with `API_BACKEND=direct` | `http-intake.logs.us5.datadoghq.com:443` (telemetry) |
| Codex | `chatgpt.com:443`, `github.com:443`, `api.github.com:443` (update check, remote config) |
| litellm without `LITELLM_LOCAL_MODEL_COST_MAP` | `raw.githubusercontent.com:443` (cost map) |

### Egress limits

- **Parallel tasks.** Each task uses one Docker network. A default Docker Desktop has address
  pools for about 29 user networks, so keep `--max-parallel` under that. When the pools are
  exhausted, the row error names `--max-parallel`, the prune command and the daemon's
  `default-address-pools` setting.
- **No upstream proxy.** The sidecar connects directly. A host that can reach the internet only
  through a corporate proxy cannot use `llm_only`.
- **Not exfiltration-proof.** The agent can still send data to an allowlisted host.
- **An exact host is a TCP destination, not a site.** A host behind a shared CDN front (for
  example `files.pythonhosted.org` or `deb.debian.org`) can serve other sites that the agent names
  in its `Host` header or TLS SNI.
- **`extra_mounts` can defeat the boundary.** Mounting the Docker socket, for example, gives the
  agent the daemon.
- **Exact hosts only.** No wildcards and no CIDR ranges.
- **podman and rootless Docker** are not tested.
- **Harbor export** refuses an `llm_only` task.
- **The sidecar is also on the default `bridge`.** A `bridge` container of another task can reach
  it. It gets no extra reach, because the allowlist is a subset of what `bridge` already reaches,
  and the sidecar serves at most 256 connections at once (one more gets `503`).
- **Plugin agents.** A third-party agent gets the API backend's hosts only when its config class
  sets `uses_api_backend = True`, and its own hosts only through `egress_hosts()`; see
  [Extending Coder Eval](EXTENDING.md#the-config-class).

### Troubleshooting egress

The sidecar log is the task's `egress.log`, beside `docker.log` (a grading container's log is
`grade.egress.log`). It is a separate file so that container output cannot add lines to it.
Each connection is one line:
`ALLOW host:port METHOD`, `DENY host:port METHOD`, `FAIL host:port <error>` (an allowed host the
sidecar could not reach), or `BAD …` (a request the proxy refused; the line names only its method
and length, because the target can carry credentials). To see which hosts a
task needed:

```bash
grep -rhE "^(ALLOW|DENY|FAIL)" --include=egress.log runs/latest | sort | uniq -c
```

Add each needed `DENY` host to `egress_allowlist`. If the host process was killed, remove the
leaked sidecars and networks:

```bash
docker rm -f $(docker ps -aq --filter label=org.coder-eval.egress)
docker network prune -f --filter label=org.coder-eval.egress
```

## Using a pre-built custom image

When your tasks need extra tools or dependencies, extend the framework image once and point tasks at
the result — no per-task build. The custom image must extend `coder-eval-agent:<version>` so it
inherits the coder-eval runtime, the entrypoint script, and the `org.coder-eval.version` label the
host's preflight check reads. (For a task whose Dockerfile can't be rebased onto Debian, use
[the runtime kit](#tasks-that-bring-their-own-base-image-the-runtime-kit-coder-eval-runtime)
instead. To have coder-eval build the image per task rather than pre-building it, see
[Building the image from a task Dockerfile](#building-the-image-from-a-task-dockerfile).)

```dockerfile
FROM coder-eval-agent:<version>          # match the version your host runs
RUN apt-get update && apt-get install -y --no-install-recommends custom-tool \
    && rm -rf /var/lib/apt/lists/*
```

```bash
docker build -t my-team/image:latest .
```

Select it either in the task YAML:

```yaml
sandbox:
  driver: docker
  docker:
    image: my-team/image:latest
```

…or from the CLI, which overrides whatever the YAML says:

```bash
coder-eval run task.yaml -D sandbox.docker.image=my-team/image:latest
```

The default when you set nothing is `coder-eval-agent:<installed package version>`.

A worked example ships in-tree: `tasks/byod_smoke_test.yaml` runs against
`templates/byod_smoke_test/Dockerfile`, which extends the framework image and drops a marker file
that the task's success criterion then asserts — proving the custom image was actually used. (The
`byod_*` names here mean "Bring Your Own **Docker**"; they are unrelated to the
[Bring Your Own Dataset](DATASETS.md) guide, which is about fanning one task out over data rows.)

```bash
make docker-image                                                   # base image first
docker build -t byod-custom-image:0.1.0 templates/byod_smoke_test/  # then the derived one
coder-eval run tasks/byod_smoke_test.yaml
```

### Troubleshooting custom images

| Symptom | Cause and fix |
| --- | --- |
| `docker: Error response from daemon: pull access denied` | The image isn't built locally and isn't pullable. Check `docker images`, then rebuild it. Docker treats an unknown local tag as a remote reference, which is why the error mentions a pull. |
| `Image <your-image> runs coder_eval <a> but the host runs <b>` | The run is refused before the container starts: the image carries an `org.coder-eval.version` label from a different coder-eval than the one installed on the host, usually inherited from a stale framework base. Rebuild the base with `make docker-image`, then rebuild your derived image with `docker build --no-cache`. To run a deliberately different image anyway (for example an unreleased build under test), set `ALLOW_IMAGE_SKEW=1` in the environment or `.env`; the mismatch then only warns, and the run gives up the reproducibility guarantee. |
| `Image <your-image> is not a coder-eval runtime image (missing the org.coder-eval.version label)` | The image doesn't descend from `coder-eval-agent` (or predates the label). Rebase it on the framework image, or use the runtime kit. `ALLOW_IMAGE_SKEW` does not bypass this. |
| `The container returned a result with no container_contract echo` or `The container did not honor the contract it was sent` | The code inside the image does not match the host's, even if its label agrees — for example an overlay image that reinstalled coder-eval, or a stale image rebuilt under the same tag. The result is refused: the record is moved to `task.json.unhonored` and a synthetic ERROR `task.json` takes its place. Rebuild or pull a matching image. `ALLOW_IMAGE_SKEW` does not bypass this. |

## Building the image from a task Dockerfile

Instead of pointing at a pre-built `image`, a task can ship its own `Dockerfile`
and have coder-eval build it before the run:

```yaml
sandbox:
  driver: docker
  docker:
    dockerfile_path: ./environment/Dockerfile   # relative to the task YAML
```

> **⚠️ Contract: a task Dockerfile MUST either start with `FROM coder-eval-agent:<version>` or use the runtime kit (see below).**
> The container runs the coder-eval orchestrator (`coder-eval _run-task-internal`)
> via the framework image's `ENTRYPOINT`. A task Dockerfile extends that image and
> adds only task-specific layers — extra `apt` packages, `COPY`-ed inputs, etc.
>
> Build the framework base first — `make docker-image` (tags both
> `coder-eval-agent:<version>` and `coder-eval-agent:latest`).

### Tasks that bring their own base image: the runtime kit (`coder-eval-runtime`)

The `FROM coder-eval-agent` contract above means a task is **rebased** onto the
Debian framework image. That breaks tasks whose Dockerfile was written for a
different base image (e.g. a Fedora recipe using `dnf`, which doesn't exist on Debian). To keep the task's own base image and build successfully, coder-eval's runtime need to be copied into the task's image. Use `make coder-eval-runtime` first to make the runtime available for copying.

```dockerfile
FROM fedora:41                 # the task's own base, kept verbatim
RUN dnf -y install ...         # the task's native recipe, runs on its own OS
# --- copy coder-eval runtime into a task image ---
COPY --from=coder-eval-runtime:latest /opt/coder-eval /opt/coder-eval
COPY --from=coder-eval-runtime:latest /usr/local/bin/coder_eval_entrypoint.sh /usr/local/bin/coder_eval_entrypoint.sh
LABEL org.coder-eval.version="<ver>"
```

`make coder-eval-runtime` builds the kit (`docker/Dockerfile.runtime`): a
standalone CPython + Node + the `coder-eval` CLI + Claude Code, all under
`/opt/coder-eval`, plus the entrypoint at the **same** `/usr/local/bin/...` path
the host pins. The kit is **glibc-only** — it runs on debian/ubuntu/fedora/rhel/…
but not musl/Alpine.

Both base images are independent and persistent — build each once and run any mix of rebase
and inject tasks without rebuilding. To build both in one shot (no credentials needed), use
**`make docker-images`** (= `make docker-image` + `make coder-eval-runtime`); reach for the
individual targets when you only need one.

> The kit installs the **no-credential** set only (core + codex), like `make docker-image` —
> it never installs the `[uipath]` extra, so there is no `make docker-images-full`. An
> inject-mode task that needs the LLMGW/`uipath` judge isn't supported by the kit as built.

```dockerfile
# environment/Dockerfile
FROM coder-eval-agent:latest          # inherit runtime + entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils
RUN pip install --no-cache-dir PyMuPDF==1.24.10
COPY input/ /root/input/
```

Behavior:

- **Path resolution** — `dockerfile_path` is resolved relative to the task YAML's
  directory at load time (with `$VAR` / `${VAR}` expansion). A missing file fails
  fast at load, not mid-run.
- **Entrypoint check** — after building, coder-eval inspects the image's
  `ENTRYPOINT` and aborts with a `FROM coder-eval-agent` hint if the runtime
  wasn't inherited.
- **Overrides `image`** — when `dockerfile_path` is set, it takes precedence over
  any `image` value.
- **Build context** — the build context is the **Dockerfile's parent directory**,
  so relative `COPY ./input/... ` instructions resolve naturally. In the layout
  above, `environment/` is the context.
- **Caching** — the image is tagged deterministically as
  `coder-eval-task-<task_id>:built`, so repeat runs of the same task reuse
  Docker's layer cache. Edit the Dockerfile and the next run rebuilds the
  changed layers only.
- **Version-checked too** — the built image inherits `org.coder-eval.version`
  from its `FROM coder-eval-agent` base, so the same preflight applies: a missing
  label, or a version that differs from the host's, refuses the run (see
  [Troubleshooting custom images](#troubleshooting-custom-images)).

A build failure aborts the task with a `DockerBuildError` (a `DockerRunError`
subclass) carrying `docker build`'s output. Because the build runs before the
run dir, `docker.log`, or `task.json` exist, the runner explicitly records the
failure so it is never a silent empty result dir: it creates the run dir, writes
the full build log to **`docker.log`**, and writes a synthetic **`task.json` with
`final_status: BUILD_FAILED`** (an `error`-category status) before re-raising. So
a failed build shows up per-task on the dashboard with its build log, exactly
where you'd look for container output.

### Customizing the build (`docker.build`)

The `docker build` invocation is configurable via `sandbox.docker.build`:

```yaml
sandbox:
  driver: docker
  docker:
    dockerfile_path: ./environment/Dockerfile
    build:
      args:                          # -> --build-arg KEY=VALUE
        PKG_VERSION: "1.2.3"
        TOKEN: "${HOST_TOKEN}"       # values are $VAR / ${VAR} expanded from the host env
      secrets:                       # -> --secret <spec> (requires BuildKit)
        - id=mytoken,env=MY_TOKEN    # forward a host env var as a build secret
        - id=npmrc,src=~/.npmrc      # or a file
      extra_args: ["--target", "runtime"]   # escape hatch for any other docker build flag
      buildkit: true                 # optional: force DOCKER_BUILDKIT (see below)
```

- **`args`** → `--build-arg KEY=VALUE`. Values are environment-expanded against
  the host. Prefer `secrets` for credentials — build-args are recorded in the
  image history.
- **`secrets`** → `--secret <spec>`. Use `id=NAME,env=VAR` to forward a host env
  var or `id=NAME,src=PATH` for a file; reference it in the Dockerfile via
  `RUN --mount=type=secret,id=NAME ...`. Secrets are exposed only to the mounting
  RUN step and never baked into layers. **Secrets require BuildKit.**
- **`extra_args`** → raw flags inserted before the build context (e.g.
  `--target`, `--network`, `--platform`). Escape hatch for options without a
  dedicated field.
- **`buildkit`** → controls the `DOCKER_BUILDKIT` env var. Omitted (default),
  coder-eval **inherits the invoker's environment** — set `DOCKER_BUILDKIT=1`
  before running coder-eval to enable it globally. Set `buildkit: true` / `false`
  to force it per task. If `secrets` are configured but BuildKit isn't enabled,
  coder-eval logs a warning (the build would otherwise fail).

The build context is always appended last, so `extra_args` can't displace it.

## Authentication

> **macOS users — read this first.** Claude Code's OAuth tokens live in the macOS Keychain. The container has no path to the Keychain, so the bundled CLI inside will return `Not logged in · Please run /login` and every task will fail at iteration 1. Before running `--driver docker`, set one of these on the host:
>
> - `ANTHROPIC_API_KEY=...` (direct Anthropic), or
> - `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_BEARER_TOKEN_BEDROCK=...` + `AWS_REGION=...` (Bedrock).
>
> Linux hosts where Claude Code stores creds under `~/.claude` already work because that directory is bind-mounted into the container.

Credentials are forwarded via `--env VAR` (name-only, never embedded in argv) for these vars when set on the host: `ANTHROPIC_API_KEY`, `API_BACKEND`, `UIPATH_*`, `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_MODEL`.

**To add one or two custom vars to the defaults (recommended)**, use `env_passthrough_extra`:

```yaml
sandbox:
  driver: docker
  docker:
    env_passthrough_extra: ["MY_CUSTOM_TOKEN", "DEBUG_FLAG"]  # Keeps all defaults + these
```

**To completely replace the list**, use `env_passthrough`:

```yaml
sandbox:
  driver: docker
  docker:
    env_passthrough: ["MY_CUSTOM_TOKEN", "ANTHROPIC_API_KEY"]
```

### `HOME` is forwarded by default

The default `env_passthrough` includes `HOME` so the in-container `~/.claude` lookup resolves at the same path as on the host (the mount lands at `$HOME/.claude` symmetrically). Practical contract:

- `Path.home()` inside the container returns the host's `HOME` value (e.g. `/Users/you` on macOS). The directory exists in the container because Docker auto-creates it as the mount parent for `~/.claude`.
- `~/.claude` is **not** the host's real dir — the runner makes a throwaway *lean copy* in a tmp dir per task and mounts that copy **read-write** at `$HOME/.claude`. The copy keeps the small set the container needs (auth via `.credentials.json`, `settings.json`, `plugins/`) and **drops heavy or transient per-session state** — `security/` (often hundreds of MB), `projects/`, `cache/`, `file-history/`, `backups/`, `downloads/`, `sessions/`, `telemetry/`, `shell-snapshots/`, `todos/`, `session-env/`, plus the volatile churn dirs the live CLI rewrites. The skip set is a denylist; the authoritative list is `CLAUDE_COPY_IGNORE` in `src/coder_eval/isolation/docker_runner.py` (a test asserts this doc and that constant agree, so the list never silently drifts). The container may write anywhere under `~/.claude`; those writes hit the copy and are discarded when the task ends — the host's real `~/.claude` is never modified. Note the copy includes the OAuth token (`.credentials.json`) and is mounted read-**write**, so the in-container agent can read and tamper with the token *copy* — contained, since the copy is discarded at task end and the host's real dir is untouched. Opt out entirely with `CODER_EVAL_NO_CLAUDE_MOUNT=1`.
- Writes under `$HOME` outside the `~/.claude` mount land in the container's ephemeral rootfs overlay. Don't expect them to persist or to be visible to the host.
- If a tool *detects platform* from `HOME` (e.g. "starts with `/Users/` → macOS"), it will draw the wrong conclusion. Vanishingly rare in practice.

Remove `HOME` from `env_passthrough` if you don't want this behavior — the container's image-default `HOME=/root` will win, but then the host's OAuth dir is no longer reachable.

## Run directory safety (`--run-dir`)

The host's run dir is bind-mounted **read-write** into the container at the same absolute path (so `task.json` and artifacts land directly on the host filesystem). This makes `--run-dir` load-bearing for isolation:

- **Do not** point `--run-dir` at a symlink. Docker resolves the source of a bind mount; following a symlink would silently grant the container RW access to a different host location.
- **Do not** point `--run-dir` at a sensitive parent (e.g. `$HOME` directly, `/etc`, a repo root). Use a dedicated `runs/` subtree.
- The default (`runs/<timestamp>/`) is safe.

## Boundary

| Layer | Location |
|---|---|
| Agent process (Claude Code SDK) | inside container |
| Sandbox + per-row criterion checking | inside container |
| **`task.json` serialization** | **container → host bind mount** |
| Per-criterion `aggregate()` (P/R/F1, suite thresholds) | host |
| Reports, run summary, experiment rollups | host |

`task.json` is the only artifact crossing the boundary. Aggregation reads it via the existing host pipeline unchanged.

## Limitations

- **Relative template paths**: `template_sources[].path` is resolved to a host absolute path *before* staging, so it won't exist inside the container unless you also forward the parent dir via `sandbox.docker.extra_mounts`.
- **No container reuse across tasks**: each task = one fresh container. Adds ~1–3 s startup overhead per task; negligible vs. LLM latency.
- **macOS Keychain auth**: not reachable from the container; set `ANTHROPIC_API_KEY` (direct) or Bedrock credentials instead.

## Architecture

The host's `DockerRunner` (`coder_eval/isolation/docker_runner.py`) renders the `docker run` argv, bind-mounts task inputs at `/work/input`, allocates an output dir at `/work/output`, and tails container stdout into `docker.log` in the task's run dir.

If the task declares a `reference:` block, a throwaway **copy** of its directory is bind-mounted **read-write** at `/work/references`, and an empty tmpfs is layered over its original location inside the task-directory mount so the agent cannot reach the solution through `$TASK_DIR`. The mount is read-write on purpose — a `:ro` bind mount cannot be chmod'd (EROFS), and the orchestrator holds this path at mode `000` for the duration of each agent turn. The container also runs with `--cap-drop DAC_OVERRIDE --cap-drop DAC_READ_SEARCH` (see the caveat below for why `FOWNER`/`CHOWN` are deliberately kept).

> **What this does and does not guarantee.** The mode-000 window stops an agent
> that *passively reads* the reference — `ls`, `cat`, `grep` all fail. Two
> things it does **not** stop:
>
> 1. **A deliberate re-chmod.** `chmod(2)` is gated on *owner-or-`CAP_FOWNER`*,
>    and the container runs as root, which owns the bind-mounted copy. Verified:
>    `chmod 755 /work/references` succeeds from inside the container and the
>    solution is then readable. Dropping `FOWNER`/`CHOWN` does **not** fix this
>    and is deliberately not done — the in-container orchestrator that *applies*
>    the window is the same root process with the same capabilities, so dropping
>    `FOWNER` breaks the harness's own `chmod` wherever the bind mount preserves
>    a non-root owner (native Linux). Verified: container root, uid-1000-owned
>    directory, `FOWNER` dropped → `chmod: Operation not permitted`. The drop
>    only bites on the hosts where it also disables the control.
> 2. **Waiting the window out.** The window spans `agent.communicate`, so
>    between turns and after the final one the reference is back at its normal
>    mode, and nothing reaps the agent's child processes at turn end — a
>    backgrounded read loop started during a turn succeeds once the window
>    closes.
>
> The *write* half of (2) IS closed: the reference tree is hashed at staging
> time and re-verified before grading, and a mismatch fails the run with
> `ReferenceTamperedError` rather than scoring `reference_comparison` against a
> file the agent wrote. **Closing the read half fully requires running the agent
> as a non-root uid that does not own the reference, and holding the window for
> the agent's whole lifetime** — tracked as follow-up. Treat the current control
> as raising the cost of accidental leakage, not as a boundary against an
> adversarial agent.

See [Reference Solutions](TASK_DEFINITION_GUIDE.md#reference-solutions).

### Two more passive-read anti-cheat blocks

These are `driver: docker` only. `driver: tempdir` shares the host uid and has no
filesystem isolation, so neither applies there (nor can — there is nothing to
mask). Both are defense-in-depth passive-read blocks, consistent with the
reference window's posture above; neither contains an adversarial agent.

- **The staged grading inputs are deleted after load.** The host stages the
  post-override `TaskDefinition` (with `success_criteria`) at `/work/input/task.yaml`
  **and** a `context.json` whose `source_yaml` is the raw task text — criteria
  verbatim, both at the top level and inside every `config_lineage` entry — for the
  in-container orchestrator to load once at startup. The agent runs in the same
  container, so leaving *either* readable would hand it the grading answer key
  (deleting only `task.yaml` leaves the identical criteria one file over in
  `context.json`). The in-container entry point deletes **both** immediately after
  they are consumed — `context.json` is parsed into memory in the command body and
  `task.yaml` by `load_task`, both before the delete (gated on
  `CODER_EVAL_IN_CONTAINER`). They are read exactly once — grading reads criteria
  from the in-memory task, never from disk. The `/work/input` mount is therefore
  read-write (a `:ro` mount rejects `rm` with EROFS). `prior.json` is kept: it is
  read later on the regrade path, and a regrade runs no agent so it is not a leak.

- **Plugins mount at a fixed container path.** The `i`-th `agent.plugins[]` entry
  is resolved on the host (a relative path against the task YAML's directory, `$VAR`
  and `~` expanded) and mounted `:ro` at `/work/plugins/<i>`. The task YAML
  staged into the container is rewritten to point at that path, so the in-container
  agent loads the directory the host mounted, whatever form the authored path took
  and whatever the container's cwd. (Before, the staged YAML kept the authored string:
  a relative or `$VAR` path the container could not resolve loaded no skill, with only
  a warning.) An entry that does not resolve to a host directory is neither mounted
  nor rewritten. It sits under `/work`, so a `sandbox.docker.extra_mounts` destination
  cannot shadow it (those are refused anywhere under `/work/`).

- **Auto-mounted plugin trees are default-deny masked.** An `agent.plugins[].path`
  (at `/work/plugins/<i>`) or a `TemplateDirSource.path` that is itself a plugin
  root (at its host path) is auto-mounted `:ro` so the plugin loads. Eval material colocated under that tree
  as siblings of the skills dir — sibling task YAMLs, reference solutions, test
  fixtures — would otherwise be readable. So the runner keeps the whole root
  mounted but layers an empty `--tmpfs` over every child dir OUTSIDE the keep-set
  (`.claude-plugin` + the manifest-declared skill dirs). Everything that is not
  the plugin surface is masked by default, so an unknown or new eval layout can
  never leak; `tests/`, `node_modules/`, and reference solutions are masked for
  free. A root agent cannot `umount` a tmpfs (`CAP_SYS_ADMIN` is not in Docker's
  default set), so this mask is *stronger* than the mode-000 reference window. Two
  residuals the mask cannot cover — an eval def or reference COLOCATED inside a
  skill dir (masking it would hide the skill), and a `task_id:` YAML **file** loose
  at the plugin root (a tmpfs masks a directory, not a single file) — are caught by
  lint rule CE068 (keep eval material out of skill dirs and off the plugin root;
  put it under a sibling `tests/`).

Inside the container, the entrypoint invokes `coder-eval _run-task-internal` (hidden subcommand), which loads the staged YAML + context, runs the standard in-process Orchestrator (driver auto-coerced back to `tempdir`), and writes `task.json` to the output mount. Host reads it and feeds the existing aggregation pipeline.

A `result_kind` discriminator on `CriterionResult` ensures `ClassificationCriterionResult` subclasses survive the JSON round-trip — without it, host-side aggregation would silently lose `observed_label`/`expected_label`.
