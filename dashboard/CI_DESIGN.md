# CI Design — Daily skills eval cron

How the nightly skills-suite eval is wired up: what runs, where it runs, how it authenticates, and which alternatives we tried and rejected on the way here.

_Last verified: 2026-04-29_ · _Owner: bai.li@uipath.com_

## Overview

Every weeknight at 23:00 PT a `systemd --user` timer on a long-lived Azure VM kicks off `daily.sh`. The wrapper pulls `main` for `coder_eval`, `skills`, and `cli`, syncs deps, runs the `skills` suite at 10x parallel via `dashboard run`, uploads the run directory to Azure Blob, ingests it into ADX, and posts a mechanical metrics summary to Slack. Auth to UiPath is via a **dedicated bot user** (`coder-eval-bot@uipath-qa.com`) signed in once interactively and refreshed in place; auth to Azure Blob and ADX is via `bai.li@uipath.com`'s `az login` plus RBAC roles. A 4-hour systemd timeout and a `flock` on `/var/lock/uip-daily.lock` prevent runaway / overlapping runs.

## Where things live

| | |
|---|---|
| VM | `coder-eval-runner` · 20.51.110.31 (static) · `Standard_D4s_v3` · Ubuntu 24.04 · westus2 · RG `rg-coder-eval-tests` |
| Repos on VM | `~/uipath/{coder_eval,skills,cli}` — all on `main` |
| Wrapper logs | `~/runs-ci/<UTC-timestamp>.log` |
| Run directory | `~/uipath/coder_eval/runs/<run-id>/` (latest symlinked at `runs/latest`) |
| Lock file | `/var/lock/uip-daily.lock` |
| Auth file | `~/.uipath/.auth` (bot user refresh token) |
| Blob | `coderevaltests/runs` · `https://coderevaltests.blob.core.windows.net/runs/<run-id>/` |
| ADX | cluster `kvc-6xx4u3sa8nz1hq7dxn.southcentralus.kusto.windows.net` · db `coder-eval-runs-db` |
| Dashboard UI | `https://flow-evalboard.uipath-dev.com/runs/<run-id>` |
| Slack | `#flow-skill-sandbox` (sandbox webhook). |
| Versioned units | `dashboard/scripts/ci/{daily.sh, slack_summary.py, coder-eval-daily.service, coder-eval-daily.timer}` |
| UiPath env | `https://alpha.uipath.com` · org `popoc` · tenant `flow_eval` |

## What runs nightly

The systemd timer fires the `coder-eval-daily.service` oneshot, which `ExecStart`s `daily.sh`. The wrapper does, in order:

1. **`git pull main`** for `coder_eval` (or whatever `BRANCH` env override sets), `skills`, `cli`. Honors `flock` (one wrapper at a time).
2. **Source `.env` files** — `~/uipath/coder_eval/.env` (bedrock keys, LLMGW, GH PAT, UV index password, optional Slack webhook) and `~/uipath/coder_eval/dashboard/.env` (ADX, Azure storage, optional storage key fallback). Done after the pull so newly-added keys upstream get picked up.
3. **Rebuild `uip` CLI** — `bun install && bun run dev:install-cli` in `~/uipath/cli`. Idempotent; recreates the bun-managed symlink chain (which can rot if a `bun install` elsewhere cleans up the global node_modules dir).
4. **Sync Python deps** — `uv pip install -e ".[dev]"` and `-e "./dashboard"` against the in-tree `.venv`.
5. **`dashboard run --suite skills`** under `flock -n -E 75`. Skills suite is 153 tasks × `claude-sonnet-4-6` × bedrock backend × `concurrency=10`. Tasks run in sandboxed tempdirs, each calling the bot-authenticated `uip` CLI for flow validation/debug.
6. **Blob upload + ADX ingest** — both wrapped in try/except inside `cli.py`. A failure prints a traceback and continues, so the Slack post still fires with metrics from `runs/latest/run.json`.
7. **Slack post** — `slack_summary.py` reads `runs/latest/run.json` and emits a JSON payload with pass/fail counts, total cost, wall duration, configured parallelism, repo SHAs, and the dashboard URL. `daily.sh` curl-POSTs to `$SLACK_WEBHOOK_URL` (no-ops if empty).

Total wall time: ~1–2 hours at 10x parallel. Peak memory: ~8 GB out of 16 GB (D4s_v3 has comfortable headroom).

## VM setup

| | |
|---|---|
| SKU | `Standard_D4s_v3` (4 vCPU, 16 GB RAM, ~$140/mo PAYG). Was originally `Standard_B2s` (4 GB) — OOM'd at concurrency=10. Resized 2026-04-29. |
| OS | Ubuntu 24.04 LTS — matches dev laptops; `uip` CLI Just Works. |
| Disk | 64 GB Premium SSD (repos + uv caches + sandbox dirs trend ~8–10 GB after a few weeks). |
| NSG | SSH (22) restricted to the office egress IP; no inbound 80/443. |
| Identity | System-assigned Managed Identity provisioned (currently unused by code; available if we migrate off user-scoped auth). |
| Linger | `loginctl enable-linger azureuser` so `systemd --user` services run without an SSH session. |

### Tooling on the box

- `uv 0.11.8`, `node v20.20.2`, `bun 1.3.13`, `gh 2.92.0`, `azure-cli 2.85.0`
- Python 3.13 inside `.venv` (managed by `uv`); system Python is 3.12 (unused).
- `uip` v1.0.0 built from source via `bun run dev:install-cli` → bun-linked at `~/.bun/bin/uip` and symlinked to `/usr/local/bin/uip` so non-interactive sshd PATH resolves it.
- Note: `npm i -g @uipath/cli` is a trap — plugins land under root-owned `/usr/lib/node_modules/@uipath/` and the maestro plugin auto-install EACCES-es. Build from source via `bun` (where plugins land in `~/.bun/install/global/`) and remove any system npm install.

## Auth model

Two independent credential surfaces: UiPath (for the eval tasks) and Azure (for blob + ADX).

### UiPath: bot user + interactive OIDC

The working path. We use a **dedicated bot user** that has gone through the one-time interactive Studio Web login required to provision a Personal Workspace and personal robot — both of which `uip flow debug` needs.

| | |
|---|---|
| Bot user | `coder-eval-bot@uipath-qa.com` · `sub: 7524a477-593e-47ce-b94a-5dce9eb78ede` |
| Mailbox | Shared `uipath-qa.com` test-account service. Login: `contact@uipath-qa.com`. |
| Tenant | Invited into the existing `popoc/flow_eval` tenant (do **not** create a new alpha org — they self-delete after 60 days). |
| Tenant roles | Automation User · Folder Administrator · Personal Workspace Administrator · "Enable user to run automations" · "Create a personal workspace" · "enable optimal Studio Web experience" |
| OIDC client | `36dea5b8-e8bb-423d-8e7b-c808df8f1c00` (the standard `uip` interactive client) |
| Auth file | `~/.uipath/.auth` on the VM — `UIPATH_ACCESS_TOKEN` (1h TTL, transparently refreshed) + `UIPATH_REFRESH_TOKEN` (one-time-use, rolling) |

#### Refresh-token lifecycle

- `AbsoluteRefreshTokenLifetime: 2592000` seconds = **30 days hard cap** on the entire RT chain (UiPath IdentityServer seed default; verified across `UiPath/IdentityServer`, `UiPath/WebhookService`, `UiPath/NotificationService`, `UiPath/ActionCenterSetup` config templates).
- `SlidingRefreshTokenLifetime` resets on each refresh, so the inactivity timer never fires for a daily-running VM.
- `RefreshTokenUsage: 1` (one-time): each refresh issues a new RT and invalidates the old one. Hence `flock` — concurrent `uip` invocations on the box can race and burn the RT.
- **Re-login required ≈ every 25 days.** Procedure in [Operate](#operate).
- The `uip login refresh` command (cli#1057, Apr 2026) gives an explicit side-effect-free refresh; useful if you want to avoid relying on incidental refresh during real work.

#### One-time bot provisioning (already done; reproduce only on a fresh tenant)

1. Create `coder-eval-bot@uipath-qa.com` via [https://uipath-qa.com/mail/](https://uipath-qa.com/mail/). Strong password (the inbox is shared — can't trust password reset). Store the password somewhere durable.
2. Sign up on alpha with that email. Invite into `popoc/flow_eval` (Admin → Accounts & Groups → Invite Users).
3. On the bot's user record, enable the tenant roles listed above.
4. Sign in as the bot via Studio Web in incognito. **This is the magic step** — it provisions the Personal Workspace + personal robot. Neither has a programmatic provisioning API.
5. Sanity-check folders include a `Personal` row:
   ```bash
   TOKEN=$(grep "^UIPATH_ACCESS_TOKEN=" ~/.uipath/.auth | cut -d= -f2)
   curl -s -H "Authorization: Bearer $TOKEN" \
     "https://alpha.uipath.com/popoc/flow_eval/orchestrator_/odata/Folders" | \
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

Tried first — would have been a cleaner CI story (no human in the credential chain). Blocked at two layers:

| | S2S External App | Bot user (interactive OIDC) |
|---|---|---|
| `sub` claim | `null` | real user UUID |
| `sub_type` | `client` | `user` |
| Personal Workspace | none — no API to create | provisioned by Studio Web login |
| Personal robot | none — `GetByUserAsync(userId)` returns `[]` | provisioned by Studio Web login |
| Refresh token | not issued (`clientCredentials.ts` omits `offline_access`) | issued |
| `flow debug` | **fails** | **works** |

The CLI's `flow debug` calls `POST /api/robotdebug/BeginSession`, which on the server runs `SetDefaultRobotAsync → GetByUserAsync(userId)`. Service account tokens have no `sub`, no User row in Orchestrator, and so `errorCode 1230 "Cannot find a personal robot configured"`. Earlier in the path, `getPersonalFolderInfo` (`maestro-sdk/src/debug-service.ts:91-94`) throws `"No personal workspace folder found"` when no `FolderType=Personal` row exists — same root cause, different layer.

Confirmed by the Orchestrator team as **by design** (Razvan Dumitru, 2026-04-27): "There's no concept around debugging via s2s." The CLI hardcodes PW targeting and doesn't accept an explicit robot in the BeginSession body. Provisioning a PW + robot for a service account would require licensing and system-folder design changes — long-tail FR, not a near-term option.

### Why not GitHub Actions self-hosted runner

Original plan, cleaner from a PR-history perspective (workflow visible in GH UI, run logs auto-archived). Blocked by **UiPath InfoSec org policy**: `/settings/actions/runners*` and `/settings/actions/runner-groups*` URLs all 404 for non-admins, and the org has not carved out a single-repo exception. Filing one is a long-tail process.

systemd-on-VM is mechanically equivalent — `daily.sh`'s body is essentially what would have lived inside a `dashboard.yml` workflow's `run:` step. We lose the UI and run-history page; we keep flock-serialized scheduling, Slack notifications, blob upload, and ADX ingest (all of which already happen inside `daily.sh` / `dashboard run`). When/if the policy flexes, the migration is mechanical.

## Slack integration

Slack post is a **Workflow Builder webhook** (not the classic Incoming Webhooks app — the latter needs workspace-admin approval in the UiPath workspace; Workflow Builder doesn't).

`daily.sh` shells out to `slack_summary.py` after `dashboard run` exits. The Python script reads `runs/latest/run.json` and emits a JSON payload like:

```
:chart_with_upwards_trend: skills suite — 2026-04-29_04-09-34 (claude-sonnet-4-6 / bedrock)
:white_check_mark: 103/153 passed (67%) · :x: 50 failed (34 fail + 16 error)
:moneybag: $113.55 · :stopwatch: 1h 46m · 10x parallel
:package: coder_eval @ aba0525 · skills @ 0126731 · cli @ 2ed060f
:bar_chart: https://flow-evalboard.uipath-dev.com/runs/2026-04-29_04-09-34
```

Pure mechanical metrics — no LLM analysis, no comparison to previous runs. The parallelism field is the configured concurrency cap from `RunSummary.max_parallel` (the suite's `BatchRunConfig.max_parallel`); older `run.json` files without that field omit the parallelism segment. On hard failure (no `run.json`) or `flock` skip (exit 75), the payload degrades to a one-liner status.

### Webhook setup (~5 min, one-time)

1. Slack → **Tools → Workflows → + New Workflow → Build Workflow**.
2. Trigger: **From a webhook**. Add one variable named `text`, type **Text**.
3. Step: **Send a message in a channel** → pick the channel → click **{ } Insert a variable** → `text` → that's the entire message body. Save.
4. Name the workflow `coder-eval-ci`. **Publish** (saving alone leaves the webhook returning `{"ok":false,"error":"workflow_not_published"}`).
5. Paste the webhook URL into `~/uipath/coder_eval/.env` → `SLACK_WEBHOOK_URL=<url>`.

**Formatting gotcha:** WfB inserts the `text` variable as **plain text** — it does *not* parse Slack mrkdwn (`*bold*`, `` `code` ``) or `<url|label>` link syntax. What still works: emoji shortcodes, line breaks (`\n`), bare URLs (Slack auto-linkifies), literal `•` bullets. For richer formatting, either add more variables and apply bold/links via the WfB toolbar inside the step, or request workspace-admin approval for the classic Incoming Webhooks app.

The webhook URL is auth-equivalent — anyone with it can post to the channel. Treat like a secret (env file only, never committed).

## Failure modes + graceful degradation

- **flock contention** (`/var/lock/uip-daily.lock` held by another `uip` invocation) → `daily.sh` exits 75; Slack posts `:warning: skipped (lock held)`.
- **Blob upload failure** (`az` not installed, RBAC revoked, key wrong) → traceback printed; run continues; ADX ingest still attempts; Slack posts metrics; dashboard URL will 404 because nothing was uploaded.
- **ADX ingest failure** (Kusto cluster unreachable, AzureCliCredential expired) → traceback printed; run continues; Slack posts metrics; row missing from ADX (backfill via `dashboard ingest <run_dir>`).
- **No `run.json`** (the eval itself crashed before completion) → Slack posts `:x: run failed before producing run.json (exit N)` with the wrapper log path.
- **Systemd `TimeoutStartSec=4h` exceeded** → systemd SIGTERMs the wrapper. Slack post may not fire.
- **VM down** → no Slack post at all (no heartbeat configured today).

## Operate

### Install on a fresh VM

After provisioning the VM, installing tools (uv, bun, azure-cli, gh), cloning the three repos to `~/uipath/{coder_eval,skills,cli}`, populating `.env` files, and completing the bot user + `az login` bootstrap:

```bash
sudo loginctl enable-linger azureuser

mkdir -p ~/.config/systemd/user
cp ~/uipath/coder_eval/dashboard/scripts/ci/coder-eval-daily.service ~/.config/systemd/user/
cp ~/uipath/coder_eval/dashboard/scripts/ci/coder-eval-daily.timer   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now coder-eval-daily.timer
systemctl --user list-timers coder-eval-daily.timer
```

### Re-login (~every 25 days)

Calendar event every 25 days. The cleanest path is to complete the OAuth flow on a workstation that has a real browser, then copy the resulting `~/.uipath/.auth` to the VM — no SSH tunnels, no headless-browser hacks.

```bash
# 1. On your laptop — fresh interactive login as the bot user
uip logout || true
uip login --interactive            # opens a browser; sign in as coder-eval-bot@uipath-qa.com

# 2. Pause the cron so it doesn't race the RT swap
ssh coder-eval-runner 'systemctl --user stop coder-eval-daily.timer'

# 3. Ship the auth file to the VM
scp ~/.uipath/.auth coder-eval-runner:~/.uipath/.auth
ssh coder-eval-runner 'chmod 600 ~/.uipath/.auth'

# 4. Confirm + resume
ssh coder-eval-runner 'cd ~/uipath/skills/tests/tasks/uipath-maestro-flow/canary && python3 canary.py'
ssh coder-eval-runner 'systemctl --user start coder-eval-daily.timer'
```

After this, **don't** run `uip` commands locally as the bot until the next rotation — the refresh token is one-time-use, so a local refresh would invalidate the copy on the VM.

If the chain dies before day 25 (policy event, accidental concurrent refresh), the recovery flow is the same — the bot password is stored durably and the `coder-eval-bot@uipath-qa.com` mailbox is persistent.

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
| Slack says "skipped (lock held)" | Find the other `uip` invocation: `ps -ef \| grep -E 'uip\|flock'` |
| Slack says "exited N — metrics from latest run.json" | `~/runs-ci/<latest>.log` for the wrapper-level error; `runs/latest/<task>/00/task.log` for per-task |
| Dashboard URL 404s | Blob upload failed; re-run `dashboard upload runs/<run_id>` after fixing auth |
| Run not in ADX | Ingest failed; re-run `dashboard ingest runs/<run_id>` after fixing auth |
