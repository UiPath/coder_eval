# CI Design — Daily skills eval cron

How the nightly skills-suite eval is wired up: what runs, where it runs, how it authenticates, and the architectural choices behind it.

_Last verified: 2026-05-20_ · _Owner: bai.li@uipath.com_

## Overview

Every weeknight at 04:00 UTC (21:00 PT prev day / 07:00 Romania) a `systemd --user` timer on a long-lived Azure VM kicks off `daily.sh`. The slot is chosen to finish ~06:00 UTC — fresh before Romania's 9 AM standup, after Bellevue's workday. The wrapper pulls `main` for `coder_eval` and `skills`, installs `@uipath/cli@alpha` from GitHub Packages (each merge to `cli/main` publishes a fresh `-alpha.<date>.<run>` prerelease under that dist-tag, so the eval tracks CLI HEAD — matching what skills smoke does in `skills/.github/workflows/smoke-skills.yml`), syncs Python deps, builds the `coder-eval-agent` docker image (skills#856 onwards, smoke-tagged tasks run inside containers), runs the `skills` suite in parallel via `dashboard run`, uploads the run directory to Azure Blob, ingests it into ADX, and (on credible full-suite runs only) posts a mechanical metrics summary to Slack. Auth to UiPath is via ROPC (`grant_type=password`) against a **dedicated bot user** (`coder-eval-bot@uipath-qa.com`) — a 50-min systemd timer mints a fresh access token from the bot's credentials stored in `.uipath-auth.env`. Auth to Azure Blob and ADX is via `bai.li@uipath.com`'s `az login` plus RBAC roles. A 4-hour systemd timeout and a `flock` on `/var/lock/uip-daily.lock` prevent runaway / overlapping runs.

## Where things live

| | |
|---|---|
| VM | `coder-eval-runner` · 20.51.110.31 (static) · `Standard_D8s_v3` (8 vCPU, 32 GB) · Ubuntu 24.04 · westus2 · RG `rg-coder-eval-tests` |
| Repos on VM | `~/uipath/{coder_eval,skills}` — both on `main`. `uip` CLI lives in `~/.npm-global/bin/`, refreshed via `npm install -g @uipath/cli@alpha` from GitHub Packages each run. |
| Wrapper logs | `~/runs-ci/<UTC-timestamp>.log` |
| Run directory | `~/uipath/coder_eval/runs/<run-id>/` (latest symlinked at `runs/latest`) |
| Lock file | `/var/lock/uip-daily.lock` |
| Auth file | `~/.uipath/.auth` (bot user access token; rewritten every 50 min by `uip-refresh-auth.timer`) |
| Auth creds | `~/uipath/coder_eval/dashboard/scripts/ci/.uipath-auth.env` (bot username + password + client + tenant; mode 600, gitignored) |
| Blob | `coderevaltests/runs` · `https://coderevaltests.blob.core.windows.net/runs/<run-id>/` |
| ADX | cluster `kvc-6xx4u3sa8nz1hq7dxn.southcentralus.kusto.windows.net` · db `coder-eval-runs-db` |
| Dashboard UI | `https://coder-evalboard.uipath-dev.com/runs/<run-id>` |
| Slack | `#flow-skill-sandbox` (sandbox webhook). |
| Versioned units | `dashboard/scripts/ci/{daily.sh, slack_summary.py, refresh-auth.sh, coder-eval-daily.service, coder-eval-daily.timer, uip-refresh-auth.service, uip-refresh-auth.timer}` |
| UiPath env | `https://alpha.uipath.com` · org `codereval` · tenant `DefaultTenant` |

## What runs nightly

The systemd timer fires the `coder-eval-daily.service` oneshot, which `ExecStart`s `daily.sh`. The wrapper does, in order:

1. **`git pull main`** for `coder_eval` (or whatever `BRANCH` env override sets) and `skills`. Honors `flock` (one wrapper at a time).
2. **Source `.env` files** — `~/uipath/coder_eval/.env` (bedrock keys, LLMGW, UV index password, `GH_NPM_REGISTRY_TOKEN`, optional Slack webhook) and `~/uipath/coder_eval/dashboard/.env` (ADX, Azure storage, optional storage key fallback). Done after the pull so newly-added keys upstream get picked up.
3. **Install `uip` CLI** — `npm install -g @uipath/cli@alpha` from GitHub Packages (`@uipath:registry=https://npm.pkg.github.com/`), run from a tempdir whose `.npmrc` carries the auth token. Matches the smoke workflow. `cli/main` publishes `-alpha.<date>.<run>` prereleases under the `alpha` dist-tag on every merge, so each nightly run picks up the freshest CLI; public npmjs `@latest` only moves on GitHub Releases (weeks behind main). Token comes from `GH_NPM_REGISTRY_TOKEN` in `.env` — a GitHub PAT with `read:packages` scope.
4. **Sync Python deps** — `uv pip install -e ".[dev]"` and `-e "./dashboard"` against the in-tree `.venv`.
5. **Docker preflight + image build** — fail fast if the daemon is unreachable; reap orphan `coder-eval-agent` containers and orphan `claude_agent_sdk/_bundled/claude` processes from prior crashed runs; then `make docker-image` (mirrors `smoke-skills.yml`). Required since skills#856 flipped the smoke-tagged tasks to `driver: docker`. Build needs only `UV_INDEX_UIPATH_PASSWORD` — Azure Artifacts accepts the PAT alone.
6. **`dashboard run --suite skills`** under `flock -n -E 75`. Skills suite is every `.yaml` under `tests/tasks/**`. The `driver: docker` tasks (smoke-tagged since skills#856) run inside `coder-eval-agent` containers; the rest run in host tempdirs. Model `claude-sonnet-4-6`, resolved at runtime to `$BEDROCK_MODEL` (the full Bedrock id — the host CLI's short aliases don't resolve inside the container); backend `bedrock`; parallelism from `BatchRunConfig.max_parallel`. Task order is shuffled per run to surface cross-task resource pollution.
7. **Blob upload + ADX ingest** — both wrapped in try/except inside `cli.py`. A failure prints a traceback and continues; metrics still land in `runs/latest/run.json` for the Slack step.
8. **Slack post (conditional)** — `slack_summary.py` reads `runs/latest/run.json` and either emits a JSON payload (pass/fail counts, total cost, wall duration, configured parallelism, repo SHAs, dashboard URL) or prints empty stdout to suppress the ping. Suppression gates: `rc != 0` (any wrapper failure, including `flock` skip), missing `run.json` (e.g. `TASK_PATTERN` smoke mode), or `tasks_run < MIN_TASKS_FOR_PING` (the constant in `slack_summary.py` — guards against test runs and broken discovery). The webhook reaches a large channel, so the policy is biased toward silent-on-doubt. `daily.sh` curl-POSTs only when stdout is non-empty (still a full no-op if `SLACK_WEBHOOK_URL` itself is empty).

Wall time and peak memory scale with task count and `max_parallel`; the VM has comfortable headroom under the current configuration.

## VM setup

| | |
|---|---|
| SKU | `Standard_D8s_v3` (8 vCPU, 32 GB RAM). Sized for `BatchRunConfig.max_parallel=20` with the docker-driver tasks each holding a full container's worth of RAM. |
| OS | Ubuntu 24.04 LTS — matches dev laptops; `uip` CLI Just Works. |
| Docker | Required since skills#856. Daemon must be reachable to the `azureuser` (member of the `docker` group). Image rebuilt fresh every run by `daily.sh`. |
| Disk | 64 GB Premium SSD (repos + uv caches + sandbox dirs trend up over time; periodic cleanup recommended). |
| NSG | SSH (22) restricted to the office egress IP; no inbound 80/443. |
| Identity | System-assigned Managed Identity provisioned (currently unused by code; available if we migrate off user-scoped auth). |
| Linger | `loginctl enable-linger azureuser` so `systemd --user` services run without an SSH session. |

### Tooling on the box

- `uv 0.11.8`, `node v20.20.2`, `npm 10.8.2`, `gh 2.92.0`, `azure-cli 2.85.0`, `jq`
- Python 3.13 inside `.venv` (managed by `uv`); system Python is 3.12 (unused).
- `uip` installed fresh each run via `npm install -g @uipath/cli@alpha` against GitHub Packages (auth via `GH_NPM_REGISTRY_TOKEN` from `.env`). Lands under `~/.npm-global/bin/uip` plus `~/.npm-global/lib/node_modules/@uipath/*` for tool plugins. The npm prefix is `~/.npm-global` (user-writable, set during VM bootstrap via `npm config set prefix`), which is what makes plain `npm install -g` work without root.

## Auth model

Two independent credential surfaces: UiPath (for the eval tasks) and Azure (for blob + ADX).

### UiPath: bot user + ROPC

A **dedicated bot user** authenticates via the OAuth2 Resource Owner Password Credentials (ROPC) grant: every 50 min a systemd timer POSTs the bot's username + password to `/identity_/connect/token` and writes the resulting access token to `~/.uipath/.auth`. Each mint is fully independent — no refresh-token chain to maintain. The bot has a Personal Workspace + personal robot (both required by `uip flow debug`), provisioned via a one-time interactive Studio Web sign-in (see *One-time bot provisioning* below).

| | |
|---|---|
| Bot user | `coder-eval-bot@uipath-qa.com` · `sub: 7524a477-593e-47ce-b94a-5dce9eb78ede` |
| Mailbox | Shared `uipath-qa.com` test-account service. Login: `contact@uipath-qa.com`. |
| Tenant | Invited into the `codereval/DefaultTenant` tenant (the dedicated CoderEval org on alpha; permanent, not subject to alpha-org auto-cleanup). |
| Tenant roles | Automation User · Folder Administrator · Personal Workspace Administrator · "Enable user to run automations" · "Create a personal workspace" · "enable optimal Studio Web experience" |
| ROPC client | `0afb5a31-3a84-4adb-ab6e-b38eee458dd4` (used as both `CLIENT_ID` and `CLIENT_SECRET` — the shared first-party password-grant client; see [coder_eval-athena](https://github.com/UiPath/coder_eval-athena/blob/main/scripts/refresh-auth.sh) for upstream provenance). Not a real secret — it's a public client identifier (clue: id == secret) and the actual auth factor is `CE_PASSWORD`. Safe to keep checked in while this repo is INTERNAL; strip if it ever flips to PUBLIC to avoid secret-scanner noise. |
| Auth file | `~/.uipath/.auth` on the VM — `UIPATH_ACCESS_TOKEN` (~1h TTL) + URL/org/tenant metadata. Rewritten in full by `refresh-auth.sh` on every refresh. |
| Auth creds | `~/uipath/coder_eval/dashboard/scripts/ci/.uipath-auth.env` — mode 600, gitignored, sourced by the systemd unit via `EnvironmentFile=`. Format in [`scripts/ci/.uipath-auth.env.example`](scripts/ci/.uipath-auth.env.example). |

#### Access-token lifecycle

- Each ROPC call returns a fresh AT (~1h TTL). The response also includes an RT, but we ignore it — fetching a new AT from username/password is just as cheap and there's no chain state to maintain.
- `uip-refresh-auth.timer` fires `refresh-auth.sh` on boot (+1 min) and every **50 min** thereafter — comfortably ahead of AT TTL. The eval-time `uip` invocations never need to refresh; they just read the up-to-date `~/.uipath/.auth`.
- Scopes are non-s2s only. For our use case (Orchestrator API + StudioWebBackend for `flow debug`), this is sufficient. The exact scope list lives in `refresh-auth.sh`.
- The bot password is the durable secret. To rotate, update `.uipath-auth.env` on the VM; the next timer fire picks it up. No calendar cadence required.

#### One-time bot provisioning (already done; reproduce only on a fresh tenant)

1. Create `coder-eval-bot@uipath-qa.com` via [https://uipath-qa.com/mail/](https://uipath-qa.com/mail/). Strong password (the inbox is shared — can't trust password reset). Store the password somewhere durable.
2. Sign up on alpha with that email. Invite into `codereval/DefaultTenant` (Admin → Accounts & Groups → Invite Users).
3. On the bot's user record, enable the tenant roles listed above.
4. Sign in as the bot via Studio Web in incognito. **This is the magic step** — it provisions the Personal Workspace + personal robot. Neither has a programmatic provisioning API.
5. Sanity-check folders include a `Personal` row:
   ```bash
   TOKEN=$(grep "^UIPATH_ACCESS_TOKEN=" ~/.uipath/.auth | cut -d= -f2)
   curl -s -H "Authorization: Bearer $TOKEN" \
     "https://alpha.uipath.com/codereval/DefaultTenant/orchestrator_/odata/Folders" | \
     python3 -c "import json,sys; [print(f['DisplayName'], '-', f['FolderType']) for f in json.load(sys.stdin)['value']]"
   ```

### Azure storage + ADX: user-scoped via `bai.li@uipath.com`

The VM is logged in to Azure CLI as `bai.li@uipath.com` (`az login --use-device-code`). Tomasz Religa granted Bai's user the two roles needed (2026-04-29):

| Resource | Role | Path |
|---|---|---|
| Storage account `coderevaltests` | Storage Blob Data Contributor | `--auth-mode login` → blob upload + pull |
| ADX database `coder-eval-runs-db` | Database Admin (Kusto-level, not Azure RBAC) | `AzureCliCredential` → schema + ingest |

**Optional fallback for blob:** `AZURE_STORAGE_KEY` env var. When set, `blob.py` and `pull-run.sh` use `--auth-mode key` instead of `--auth-mode login`. Useful for environments where `az login` isn't available; documented in `dashboard/.env.example`.

**Failure modes if either token chain expires:** blob upload + ADX ingest are wrapped in try/except. A run still completes, posts to Slack, and lands in `runs/`; it just doesn't appear on the dashboard / in Kusto. Recovery is `az login --use-device-code` on the VM.

### Why not S2S service account

An S2S external app would be a cleaner CI story (no human in the credential chain), but is blocked at two layers:

| | S2S External App | Bot user (ROPC) |
|---|---|---|
| `sub` claim | `null` | real user UUID |
| `sub_type` | `client` | `user` |
| Personal Workspace | none — no API to create | provisioned by one-time Studio Web login |
| Personal robot | none — `GetByUserAsync(userId)` returns `[]` | provisioned by one-time Studio Web login |
| `flow debug` | **fails** | **works** |

The CLI's `flow debug` calls `POST /api/robotdebug/BeginSession`, which on the server runs `SetDefaultRobotAsync → GetByUserAsync(userId)`. Service account tokens have no `sub`, no User row in Orchestrator, and so `errorCode 1230 "Cannot find a personal robot configured"`. Earlier in the path, `getPersonalFolderInfo` (`maestro-sdk/src/debug-service.ts:91-94`) throws `"No personal workspace folder found"` when no `FolderType=Personal` row exists — same root cause, different layer.

Confirmed by the Orchestrator team as **by design** (Razvan Dumitru, 2026-04-27): "There's no concept around debugging via s2s." The CLI hardcodes PW targeting and doesn't accept an explicit robot in the BeginSession body. Provisioning a PW + robot for a service account would require licensing and system-folder design changes — long-tail FR, not a near-term option.

ROPC works because the access token carries the bot user's `sub` (so PW/personal-robot resolution works on the server), while the credential chain is just a username + password in `.uipath-auth.env`. Scope limitation: non-s2s only, which is sufficient for our workload.

### Why not GitHub Actions self-hosted runner

A self-hosted runner would be cleaner from a PR-history perspective (workflow visible in GH UI, run logs auto-archived). Blocked by **UiPath InfoSec org policy**: `/settings/actions/runners*` and `/settings/actions/runner-groups*` URLs all 404 for non-admins, and the org has not carved out a single-repo exception. Filing one is a long-tail process.

systemd-on-VM is mechanically equivalent — `daily.sh`'s body is essentially what would live inside a `dashboard.yml` workflow's `run:` step. We lose the UI and run-history page; we keep flock-serialized scheduling, Slack notifications, blob upload, and ADX ingest (all of which happen inside `daily.sh` / `dashboard run`). When/if the policy flexes, the migration is mechanical.

## Slack integration

Slack post is a **Workflow Builder webhook** (not the classic Incoming Webhooks app — the latter needs workspace-admin approval in the UiPath workspace; Workflow Builder doesn't).

`daily.sh` shells out to `slack_summary.py` after `dashboard run` exits. The Python script reads `runs/latest/run.json` and emits a JSON payload like:

```
:chart_with_upwards_trend: skills suite — <run-id> (claude-sonnet-4-6 / bedrock)
:white_check_mark: <pass>/<run> passed (<pct>%) · :x: <fail> failed (<f> fail + <e> error)
:moneybag: $<cost> · :stopwatch: <wall> · <N>x parallel
:package: coder_eval @ <sha> · skills @ <sha> · uip @ <ver>
:bar_chart: https://coder-evalboard.uipath-dev.com/runs/<run-id>
```

Pure mechanical metrics — no LLM analysis, no comparison to previous runs. The parallelism line is the configured cap from `RunSummary.max_parallel`; the line is omitted when the field is absent.

**Suppression policy** (also in nightly step 8 above): the script prints empty stdout — which `daily.sh` treats as a no-op — when any of these gates trip:

| Gate | Why suppress |
|---|---|
| `rc != 0` | Wrapper failure of any kind (lock conflict, crash, partial run). Metrics would be misleading; investigate the wrapper log instead. |
| `runs/latest/run.json` missing | E.g. `TASK_PATTERN` single-task smoke that bypasses the dashboard wrapper. Nothing meaningful to summarize. |
| `tasks_run < MIN_TASKS_FOR_PING` | The constant in `slack_summary.py`. Guards against broken task discovery and ad-hoc test runs. |

The webhook reaches a large audience, so the policy is biased toward silent-on-doubt. Surfacing failures is the wrapper log's job, not Slack's.

### Webhook setup (~5 min, one-time)

1. Slack → **Tools → Workflows → + New Workflow → Build Workflow**.
2. Trigger: **From a webhook**. Add one variable named `text`, type **Text**.
3. Step: **Send a message in a channel** → pick the channel → click **{ } Insert a variable** → `text` → that's the entire message body. Save.
4. Name the workflow `coder-eval-ci`. **Publish** (saving alone leaves the webhook returning `{"ok":false,"error":"workflow_not_published"}`).
5. Paste the webhook URL into `~/uipath/coder_eval/.env` → `SLACK_WEBHOOK_URL=<url>`.

**Formatting gotcha:** WfB inserts the `text` variable as **plain text** — it does *not* parse Slack mrkdwn (`*bold*`, `` `code` ``) or `<url|label>` link syntax. What still works: emoji shortcodes, line breaks (`\n`), bare URLs (Slack auto-linkifies), literal `•` bullets. For richer formatting, either add more variables and apply bold/links via the WfB toolbar inside the step, or request workspace-admin approval for the classic Incoming Webhooks app.

The webhook URL is auth-equivalent — anyone with it can post to the channel. Treat like a secret (env file only, never committed).

## Failure modes + graceful degradation

Slack is intentionally silent on failure (see the suppression policy in the Slack section). Investigate via the wrapper log + `journalctl`, not the channel.

- **Docker daemon unreachable** → `daily.sh` preflight fails the run before the dashboard wrapper starts. Wrapper log carries the error.
- **flock contention** (`/var/lock/uip-daily.lock` held by another `uip` invocation) → `daily.sh` exits 75. Slack suppressed (rc != 0).
- **Blob upload failure** (`az` not installed, RBAC revoked, key wrong) → traceback printed; run continues; ADX ingest still attempts; Slack posts metrics if `rc == 0` and `tasks_run` is above the floor; dashboard URL will 404 because nothing was uploaded.
- **ADX ingest failure** (Kusto cluster unreachable, AzureCliCredential expired) → traceback printed; run continues; Slack posts metrics on the same conditions; row missing from ADX (backfill via `dashboard ingest <run_dir>`).
- **No `run.json`** (the eval itself crashed before completion) → Slack suppressed. `~/runs-ci/<latest>.log` carries the wrapper-level error.
- **Systemd `TimeoutStartSec=4h` exceeded** → systemd SIGTERMs the wrapper. Slack suppressed (rc != 0).
- **VM down** → no run, no Slack post (no heartbeat configured today).

## Operate

### Install on a fresh VM

After provisioning the VM, installing tools (uv, node/npm, azure-cli, gh, jq), configuring a user-writable npm prefix (`npm config set prefix ~/.npm-global`), cloning the two repos to `~/uipath/{coder_eval,skills}`, populating the daily `.env` files (including `GH_NPM_REGISTRY_TOKEN` — a GitHub PAT with `read:packages` scope, used by `daily.sh` to install `@uipath/cli@alpha` from GitHub Packages), and completing the bot user + `az login` bootstrap:

```bash
# 1. Drop the ROPC credentials (mode 600, never committed)
CI_DIR=~/uipath/coder_eval/dashboard/scripts/ci
cp "$CI_DIR/.uipath-auth.env.example" "$CI_DIR/.uipath-auth.env"
# edit to put the real CE_PASSWORD in (the rest of the values match the example)
chmod 600 "$CI_DIR/.uipath-auth.env"

# 2. Sanity-check the refresh script works manually
set -a && source "$CI_DIR/.uipath-auth.env" && set +a
"$CI_DIR/refresh-auth.sh"
test -s ~/.uipath/.auth && echo "auth ok"

# 3. Install systemd units
sudo loginctl enable-linger azureuser

mkdir -p ~/.config/systemd/user
cp ~/uipath/coder_eval/dashboard/scripts/ci/coder-eval-daily.service   ~/.config/systemd/user/
cp ~/uipath/coder_eval/dashboard/scripts/ci/coder-eval-daily.timer     ~/.config/systemd/user/
cp ~/uipath/coder_eval/dashboard/scripts/ci/uip-refresh-auth.service   ~/.config/systemd/user/
cp ~/uipath/coder_eval/dashboard/scripts/ci/uip-refresh-auth.timer     ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now coder-eval-daily.timer
systemctl --user enable --now uip-refresh-auth.timer
systemctl --user list-timers coder-eval-daily.timer uip-refresh-auth.timer
```

### Rotate the bot password

The bot password is the durable secret. To rotate (or recover from a compromise):

1. Reset the password via [https://uipath-qa.com/mail/](https://uipath-qa.com/mail/) (inbox `contact@uipath-qa.com`) and stash the new value somewhere durable.
2. Update `~/uipath/coder_eval/dashboard/scripts/ci/.uipath-auth.env` on the VM (`CE_PASSWORD=` line).
3. `systemctl --user start uip-refresh-auth.service` to force an immediate mint.
4. `cat ~/.uipath/.auth` to confirm the new `UIPATH_ACCESS_TOKEN`.

No cron pause needed — ROPC has no chain to race against. Each mint is independent.

### Trigger an ad-hoc run

```bash
systemctl --user start coder-eval-daily.service
journalctl --user -u coder-eval-daily.service -f
```

### Override branch or suite for one run

```bash
systemctl --user edit coder-eval-daily.service       # add Environment=BRANCH=<branch>, SUITE=<name>, etc.
systemctl --user daemon-reload
systemctl --user start coder-eval-daily.service
```

Single-task smoke mode: set `TASK_PATTERN=path/to/task.yaml` to skip the dashboard wrapper and run `coder-eval run` directly (no upload/ingest, no analysis).

### Diagnose a failed run

| Symptom | Where to look |
|---|---|
| No Slack post | `journalctl --user -u coder-eval-daily.service -n 200` |
| Slack quiet but run expected | Expected behavior when the run failed, was a `TASK_PATTERN` smoke, or had `tasks_run < MIN_TASKS_FOR_PING`. Check `journalctl --user -u coder-eval-daily.service` and the wrapper log to confirm. |
| Lock conflict suspected | Find the other `uip` invocation: `ps -ef \| grep -E 'uip\|flock'` |
| Wrapper exited non-zero but tasks did run | `~/runs-ci/<latest>.log` for the wrapper-level error; `runs/latest/<task>/00/task.log` for per-task |
| Dashboard URL 404s | Blob upload failed; re-run `dashboard upload runs/<run_id>` after fixing auth |
| Run not in ADX | Ingest failed; re-run `dashboard ingest runs/<run_id>` after fixing auth |
