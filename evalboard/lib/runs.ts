import { promises as fs } from "node:fs";
import path from "node:path";
import {
    LOCAL_RUNS_DIR,
    ensureRunAnalysis,
    ensureRunDir,
    ensureRunMeta,
    ensureRunSummary,
    ensureTaskDir,
    isValidId,
    isValidTaskId,
    listRunIdsRemote,
} from "./blob";
import { DELIVERABLE_KINDS, DELIVERABLE_NAMES } from "./artifact-kinds";
import { messageCostUsd } from "./pricing";

// Resolution order:
//   1. EVALBOARD_LOCAL_RUNS_DIR — local mode, points at a coder_eval runs dir
//      (no blob, no caching).
//   2. EVALBOARD_RUNS_DIR — blob-mode cache override, used when process.cwd()
//      is read-only (e.g., App Service Run From Package).
//   3. ./runs-remote — default blob-mode cache.
export const RUNS_DIR = LOCAL_RUNS_DIR
    ? path.resolve(LOCAL_RUNS_DIR)
    : (process.env.EVALBOARD_RUNS_DIR ??
      path.resolve(process.cwd(), "runs-remote"));

// ---------- Types ----------

export interface ComponentSha {
    name: string; // "coder_eval" | "skills" | "cli"
    sha: string;
    // null when value isn't a real commit (e.g. "unknown", or a package tag
    // like "alpha" since #276 switched to consuming GitHub Packages instead
    // of building from source).
    url: string | null;
}

// Git SHAs are hex, 7-40 chars. Non-matching strings are version labels
// ("alpha", "stable", "v1.2.3") and shouldn't be linked to github.com/.../tree/<x>.
const SHA_RE = /^[0-9a-f]{7,40}$/i;

// A run is one `coder-eval run` invocation. It contains N task results.
// Run-level summary stats. Fields aggregate across all tasks in the run.
export interface RunSummary {
    id: string;
    startTime: string | null;
    endTime: string | null;
    // Sum of per-task durations (compute time). Falls back to wall-clock
    // (run end - run start) when per-task durations are unavailable.
    taskDurationSeconds: number | null;
    tasksRun: number;
    tasksSucceeded: number;
    tasksFailed: number;
    tasksError: number;
    totalCostUsd: number | null;
    componentShas: ComponentSha[];
}

export interface TaskResultSummary {
    taskId: string;
    status: string | null;
    weightedScore: number | null;
    durationSeconds: number | null;
    totalCostUsd: number | null;
    actualCommands: number | null;
    totalTurns: number | null;
    expectedTurns: number | null;
    // True when the agent's final iteration emitted a text reply
    // (i.e. ResultMessage.result was non-empty). Lets grid/trends
    // Turns cells inflate by +1 on legacy runs that lack total_turns.
    hasFinalReply: boolean;
    // Per-task token totals from run.json. Null on legacy runs that
    // don't record per-task token counts.
    outputTokens: number | null;
    cacheCreationTokens: number | null;
    cacheReadTokens: number | null;
    // Model the task ran on (run.json `model_used`). Used to price token
    // buckets as USD for the Tokens↔USD column toggle. Null on legacy runs.
    model: string | null;
    tags: string[];
    // Derived primary group. See deriveSkill below for the resolution chain
    // (new runs use task_path; older runs fall back to a tag heuristic).
    skill: string | null;
}

export interface CriterionResult {
    criterionType: string | null;
    description: string | null;
    score: number | null;
    details: string | null;
    error: string | null;
}

export interface ElementExecution {
    elementId: string;
    elementType: string | null;
    status: string;
    startedAt: string | null;
    completedAt: string | null;
    errorMessage: string | null;
    outputPreview: string | null;
}

export interface FlowDebugResult {
    finalStatus: string | null;
    studioWebUrl: string | null;
    jobKey: string | null;
    elements: ElementExecution[];
}

export interface ToolCall {
    index: number;
    tool: string;
    summary: string;
}

// One assistant message in the SDK transcript. Captured per `iterations[i].
// messages[j]` and used by the message-timeline view. Includes:
//   - timing: generation_duration_ms, and any captured tool executions that
//     this message kicked off (matched by tool_use_id).
//   - content: a flattened list of typed blocks (thinking / tool_use / text).
export interface MessageEvent {
    index: number;                // 1-based order across the whole task
    role: "assistant";            // user/system live in iteration metadata, not here
    startedAt: string | null;
    completedAt: string | null;
    generationMs: number | null;  // LLM generation time for this message
    thinkingMs: number | null;    // portion of generationMs attributable to thinking blocks
    textMs: number | null;        // portion of generationMs attributable to text blocks
    toolGenMs: number | null;     // portion of generationMs attributable to tool_use blocks
    blockTypes: ("thinking" | "tool_use" | "text")[];
    thinkingText: string | null;  // concatenated thinking blocks
    text: string | null;          // concatenated text blocks (final reply chunks)
    toolUses: MessageToolUse[];   // resolved against iteration.commands by tool_use_id
    // Per-message token usage from the Anthropic API. Summed across grouped
    // raws (one logical emission may span multiple MessageEntry rows when
    // the CLI splits content blocks). null when no raw in the group carried
    // a usage figure — older runs predating per-message tokens.
    inputTokens: number | null;
    outputTokens: number | null;
    cacheWriteTokens: number | null;
    cacheReadTokens: number | null;
    // Branch identity for the prompt-cache cascade tree: the Task tool_use_id
    // that spawned this message's sub-agent, null for the main thread, or
    // `undefined` when the run never recorded it (legacy). Calls grouped by this
    // value form a branch whose context is re-read only within itself — a
    // sub-agent's tokens don't cascade into the main thread or siblings.
    parentToolUseId: string | null | undefined;
    // Anthropic's `reasoning_tokens`. ~Always 0 from the SDK, so it is NOT
    // used to attribute thinking output — see thinkingOutputTokens. Kept for
    // completeness. null when no raw in the group recorded it.
    reasoningTokens: number | null;
    // Output tokens attributed to the thinking block(s), taken from the real
    // per-emission output_tokens of the thinking emission (the agent splits a
    // call's output across its blocks by content length). null when no output
    // was recorded or the group has no thinking block.
    thinkingOutputTokens: number | null;
    // Output tokens attributed to the text block(s) — the text emission's own
    // recorded output_tokens. Null when there's no output figure or no text block.
    textOutputTokens: number | null;
    // Model id on the emission (e.g. "claude-sonnet-4-6"). Consumed by the
    // cascade-aware thinking-cost simulator to price each message. null when
    // no raw in the group recorded it.
    model: string | null;
    // True cost in USD for this message's API call, priced from the per-message
    // token buckets and model against the shared rate table (lib/pricing.ts).
    // The SDK only reports a cumulative per-turn cost, so this is the
    // rate-accurate per-message attribution. null when the model is unpriced or
    // no token figure was recorded (older runs).
    costUsd: number | null;
}

export interface MessageToolUse {
    toolName: string;
    toolUseId: string | null;
    // Human-friendly summary (description-first). Kept for compatibility with
    // anything that already consumes the old single-string display.
    summary: string;
    // The actual operative argument the agent passed: shell command for Bash,
    // file_path for Read/Write/Edit, skill id for Skill, pattern for Grep, etc.
    // This is what you'd run to reproduce the call. Null only when no
    // recognizable key was present.
    argText: string | null;
    // The optional `description` field on the tool call — agent's stated intent.
    // Only set when distinct from argText and present in the params.
    description: string | null;
    genMs: number | null;         // LLM generation time for this tool_use block
    durationMs: number | null;    // tool execution time (separate from generationMs)
    isError: boolean;
    resultPreview: string | null; // short truncated preview of the result
    // Output tokens for this tool_use — the tool emission's recorded
    // output_tokens (the agent records output per block-emission). Only when a
    // single emission carries multiple parallel tool_uses is it split among
    // them (by arg-size proxy). null when no output_tokens was recorded.
    outputTokens: number | null;
}

export interface ArtifactRef {
    relPath: string;
    // Lowercased file extension without the dot ("flow", "md", "json"), or
    // "file" when the name has no extension. Drives the KindChip label/color.
    kind: string;
    sizeBytes: number;
}

export interface TaskDetail extends TaskResultSummary {
    runId: string;
    finalStatus: string | null;
    errorMessage: string | null;
    taskDescription: string | null;
    criteria: CriterionResult[];
    artifacts: ArtifactRef[];
    flowDebug: FlowDebugResult | null;
    toolCalls: ToolCall[];
    // Final text-only assistant message (no tool call) — sourced from the
    // last turn's ResultMessage.result. Renders as the trailing entry in
    // the Turn timeline so the (5) header reconciles with the 4 tool calls.
    finalAssistantText: string | null;
    // Per-assistant-message timeline data. Empty array for legacy task.json
    // files that don't have iterations[].messages; the message-timeline
    // section then doesn't render.
    messages: MessageEvent[];
    // Per-task token totals summed across iterations. All zeros for legacy
    // runs that don't record per-iteration token_usage.
    tokens: TokenTotals;
    // Per-sub-agent token breakdown keyed by the spawning Agent tool_use_id
    // (from iterations[].sub_agent_usage). Sourced from the Agent tool-result's
    // tool_use_result.usage, so it is COMPLETE — total = input + output +
    // cache-creation + cache-read. Used to attribute tokens to the Agent call
    // that spawned a sub-agent. Empty for runs that predate this capture.
    subAgentUsageByToolId: Record<string, SubAgentTotals>;
}

// Full per-sub-agent token breakdown (all components, cache-read included).
export interface SubAgentTotals {
    total: number;
    input: number;
    output: number;
    cacheCreation: number;
    cacheRead: number;
}

// ---------- run.json schema ----------

interface RawTaskResult {
    task_id?: string;
    status?: string;
    weighted_score?: number;
    duration?: number;
    total_cost_usd?: number;
    input_tokens?: number | null;
    output_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
    cache_read_input_tokens?: number | null;
    actual_commands?: number;
    // Cumulative SDK turn count + configured target. Absent on runs from
    // before the dashboard-expected-turns PR; both fields are optional and
    // null-fallback through the cell helpers in lib/turns.ts.
    total_turns?: number;
    expected_turns?: number | null;
    // Documented visible-turn count (tool calls + final reply) — the canonical
    // metric the "within expected turns" chart compares against expected_turns.
    // Absent on runs predating this field; visibleTurnsFromRaw() then reconstructs
    // it from actual_commands + has_final_reply (the identical formula) so the
    // metric still populates for historical runs.
    visible_turns?: number | null;
    // True iff the final iteration's ResultMessage.result was non-empty.
    // Absent on legacy runs predating the field — treated as false.
    has_final_reply?: boolean;
    tags?: string[];
    // Source YAML path. Persisted by coder-eval starting with the
    // task_path PR; absent on older runs (deriveSkill falls back to tags).
    task_path?: string | null;
    // Model the task ran on (e.g. "claude-sonnet-4-6"). Used to price token
    // buckets as USD. Absent on legacy runs.
    model_used?: string | null;
}

interface RawRunJson {
    run_id?: string;
    start_time?: string;
    end_time?: string;
    total_duration_seconds?: number;
    tasks_run?: number;
    tasks_succeeded?: number;
    tasks_failed?: number;
    tasks_error?: number;
    task_results?: RawTaskResult[];
    // Values are scalars except `tool_plugins`, a {plugin: version} map of
    // the installed @uipath/*-tool packages (recorded since coder_eval #366).
    environment_info?: Record<
        string,
        string | number | null | Record<string, string>
    >;
}

// Components captured in run env_info. Each entry may accept multiple keys —
// the CLI shipped as `cli_git_commit` (when built from source) until
// coder_eval #276 switched the runner to consume @uipath/cli@alpha from
// GitHub Packages, after which it's `cli_version` (output of `uip --version`,
// e.g. "0.1.21-alpha.234"). New keys come first so they win when both
// happen to be present. `nonShaUrl` is the link target used when the env
// value isn't a SHA (e.g. an npm package version) — null means no link.
const COMPONENTS: {
    display: string;
    repo: string | null;
    nonShaUrl: string | null;
    keys: string[];
}[] = [
    {
        display: "coder_eval",
        repo: "UiPath/coder_eval",
        nonShaUrl: null,
        keys: ["git_commit"],
    },
    {
        display: "skills",
        repo: "UiPath/skills",
        nonShaUrl: null,
        keys: ["skills_git_commit"],
    },
    {
        display: "cli",
        repo: "UiPath/cli",
        nonShaUrl: "https://github.com/UiPath/cli/pkgs/npm/cli/versions",
        keys: ["cli_version", "cli_git_commit"],
    },
];

export function extractComponentShas(
    env: RawRunJson["environment_info"],
): ComponentSha[] {
    if (!env) return [];
    const out: ComponentSha[] = [];
    for (const comp of COMPONENTS) {
        let value: string | null = null;
        for (const k of comp.keys) {
            const v = env[k];
            if (typeof v === "string" && v) {
                value = v;
                break;
            }
        }
        if (value == null) continue;
        let url: string | null = null;
        if (comp.repo && SHA_RE.test(value)) {
            url = `https://github.com/${comp.repo}/tree/${value}`;
        } else if (comp.nonShaUrl) {
            url = comp.nonShaUrl;
        }
        out.push({ name: comp.display, sha: value, url });
    }
    // Tool plugins (e.g. maestro-tool) version independently of the cli
    // shell, and the plugin's bundled schema — not the shell version — often
    // decides behavior; surface each one as its own chip after `cli`.
    // Published from the same GitHub Packages feed as @uipath/cli.
    const plugins = env.tool_plugins;
    if (plugins != null && typeof plugins === "object") {
        for (const [name, version] of Object.entries(plugins).sort(([a], [b]) =>
            a.localeCompare(b),
        )) {
            if (typeof version !== "string" || !version) continue;
            out.push({
                name,
                sha: version,
                url: `https://github.com/UiPath/cli/pkgs/npm/${encodeURIComponent(name)}/versions`,
            });
        }
    }
    return out;
}

// ---------- Readers ----------

export async function listRunIds(): Promise<string[]> {
    return listRunIdsRemote();
}

export async function latestRunId(): Promise<string | null> {
    const ids = await listRunIds();
    return ids[0] ?? null;
}

async function readJson<T>(p: string): Promise<T | null> {
    try {
        const raw = await fs.readFile(p, "utf-8");
        return JSON.parse(raw) as T;
    } catch {
        return null;
    }
}

async function readRunJson(id: string): Promise<RawRunJson | null> {
    await ensureRunSummary(id, RUNS_DIR);
    return readJson<RawRunJson>(path.join(RUNS_DIR, id, "run.json"));
}

// Resolve the skill (primary grouping axis) for a task. Two-stage fallback:
//   1. New runs: parse "tasks/<skill>/..." from task_path — the folder a task
//      lives under in the skills repo is the authoritative skill name.
//   2. Older runs (or any task without a recognisable task_path): pick the
//      first skill-shaped tag. Convention in the skills repo: every task
//      lists its folder name as the first tag, and skill folders are
//      either "activation" or "uipath-*". The prefix check filters out
//      generic tags like "smoke"/"e2e" that may appear if a task is
//      missing the convention.
// Returns null when neither signal yields anything; the UI buckets these
// under an "unknown" group.
function deriveSkill(
    taskPath: string | null | undefined,
    tags: string[] | null | undefined,
): string | null {
    if (taskPath) {
        const m = taskPath.match(/(?:^|\/)tasks\/([^/]+)\//);
        if (m) return m[1];
    }
    for (const t of tags ?? []) {
        if (t === "activation" || t.startsWith("uipath-")) return t;
    }
    return null;
}

export function toTaskRow(t: RawTaskResult): TaskResultSummary {
    const tags = t.tags ?? [];
    return {
        taskId: t.task_id ?? "",
        status: t.status ?? null,
        weightedScore: t.weighted_score ?? null,
        durationSeconds: t.duration ?? null,
        totalCostUsd: t.total_cost_usd ?? null,
        actualCommands: t.actual_commands ?? null,
        totalTurns: t.total_turns ?? null,
        expectedTurns: t.expected_turns ?? null,
        hasFinalReply: t.has_final_reply ?? false,
        outputTokens: t.output_tokens ?? null,
        cacheCreationTokens: t.cache_creation_input_tokens ?? null,
        cacheReadTokens: t.cache_read_input_tokens ?? null,
        model: t.model_used ?? null,
        tags,
        skill: deriveSkill(t.task_path, tags),
    };
}

export async function readRunSummary(id: string): Promise<RunSummary | null> {
    const data = await readRunJson(id);
    if (!data) return null;
    const taskResults = data.task_results ?? [];
    const totalCost = taskResults.reduce(
        (a, t) => a + (t.total_cost_usd ?? 0),
        0,
    );
    // Sum of per-task durations (compute time). Only use the sum when every
    // task has a duration recorded; otherwise the partial sum would understate
    // the run drastically (e.g. 1/50 tasks with a duration would render as that
    // single task's time). Fall back to wall-clock in that case.
    const taskDurationSum = taskResults.reduce(
        (a, t) => a + (t.duration ?? 0),
        0,
    );
    const allHaveDuration =
        taskResults.length > 0 &&
        taskResults.every((t) => t.duration != null);
    const taskDurationSeconds = allHaveDuration
        ? taskDurationSum
        : (data.total_duration_seconds ?? null);
    return {
        id,
        startTime: data.start_time ?? null,
        endTime: data.end_time ?? null,
        taskDurationSeconds,
        tasksRun: data.tasks_run ?? taskResults.length,
        tasksSucceeded: data.tasks_succeeded ?? 0,
        tasksFailed: data.tasks_failed ?? 0,
        tasksError: data.tasks_error ?? 0,
        totalCostUsd: taskResults.length ? totalCost : null,
        componentShas: extractComponentShas(data.environment_info),
    };
}

export async function readRunTasks(
    id: string,
): Promise<TaskResultSummary[] | null> {
    const data = await readRunJson(id);
    if (!data) return null;
    return (data.task_results ?? []).filter((t) => t.task_id).map(toTaskRow);
}

// Minimal per-task / per-run projection used by the front-page overview
// (daily success chart + tag rails). Per-task detail is needed so that a
// tag filter can scope success rate to only matching tasks within a run.
export interface RunOverviewTask {
    taskId: string;
    status: string | null;
    tags: string[];
    skill: string | null;
    totalCostUsd: number | null;
    durationSeconds: number | null;
    weightedScore: number | null;
    actualCommands: number | null;
    totalTurns: number | null;
    expectedTurns: number | null;
    visibleTurns: number | null;
    hasFinalReply: boolean;
}

export interface RunOverview {
    id: string;
    tasks: RunOverviewTask[];
    // Whole-run aggregates (mirrors readRunSummary). Carried here so the
    // front-page table and the chart can be built from a single read.
    totalCostUsd: number | null;
    taskDurationSeconds: number | null;
    componentShas: ComponentSha[];
}

// Visible-turn count for a task row: the persisted `visible_turns` field when
// present, else reconstructed as actual_commands + (1 if final reply). That
// reconstruction is the documented turn rule (tool calls + final reply, see
// docs/features/2026-05-22-visible-turns.md) and is provably identical to the
// persisted field — so it backfills the "within expected turns" metric for runs
// written before `visible_turns` existed. null when neither signal is present.
export function visibleTurnsFromRaw(t: {
    visible_turns?: number | null;
    actual_commands?: number | null;
    has_final_reply?: boolean;
}): number | null {
    if (t.visible_turns != null) return t.visible_turns;
    if (t.actual_commands != null) {
        return t.actual_commands + (t.has_final_reply ? 1 : 0);
    }
    return null;
}

export async function readRunOverview(
    id: string,
): Promise<RunOverview | null> {
    const data = await readRunJson(id);
    if (!data) return null;
    const taskResults = data.task_results ?? [];
    const tasks: RunOverviewTask[] = taskResults
        .filter((t) => t.task_id)
        .map((t) => {
            const tags = t.tags ?? [];
            return {
                taskId: t.task_id ?? "",
                status: t.status ?? null,
                tags,
                skill: deriveSkill(t.task_path, tags),
                totalCostUsd: t.total_cost_usd ?? null,
                durationSeconds: t.duration ?? null,
                weightedScore: t.weighted_score ?? null,
                actualCommands: t.actual_commands ?? null,
                totalTurns: t.total_turns ?? null,
                expectedTurns: t.expected_turns ?? null,
                visibleTurns: visibleTurnsFromRaw(t),
                hasFinalReply: t.has_final_reply ?? false,
            };
        });
    const totalCost = taskResults.reduce(
        (a, t) => a + (t.total_cost_usd ?? 0),
        0,
    );
    const taskDurationSum = taskResults.reduce(
        (a, t) => a + (t.duration ?? 0),
        0,
    );
    const allHaveDuration =
        taskResults.length > 0 &&
        taskResults.every((t) => t.duration != null);
    return {
        id,
        tasks,
        totalCostUsd: taskResults.length ? totalCost : null,
        taskDurationSeconds: allHaveDuration
            ? taskDurationSum
            : (data.total_duration_seconds ?? null),
        componentShas: extractComponentShas(data.environment_info),
    };
}

// Files matching any pattern are hidden from the Artifacts list — they're
// reconstructible build artifacts, local state, or secrets, not deliverables.
// Keep in sync with _EXCLUDE_PATTERNS in dashboard/src/dashboard/blob.py: the
// upload filter and this display filter must agree on what counts as noise.
export const ARTIFACT_EXCLUDE_PATTERNS = [
    "*/.venv/*",
    "*/__pycache__/*",
    "*.pyc",
    "*/bin/*",
    "*/obj/*",
    "*.dll",
    "*.nupkg",
    "*.pdb",
    "*/node_modules/*",
    "*/.npm-prefix/*",
    "*.lock",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.env",
];

// fnmatch semantics (mirrors Python's fnmatch in blob.py): `*` matches any run
// of characters including `/`, `?` matches one. Everything else is literal.
function globToRegExp(glob: string): RegExp {
    const escaped = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    return new RegExp(`^${escaped.replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
}

const EXCLUDE_RES = ARTIFACT_EXCLUDE_PATTERNS.map(globToRegExp);

export function isExcludedArtifact(relPath: string): boolean {
    return EXCLUDE_RES.some((re) => re.test(relPath));
}

function artifactRank(a: ArtifactRef): number {
    const base = (a.relPath.split("/").pop() ?? a.relPath).toLowerCase();
    return DELIVERABLE_KINDS.has(a.kind) || DELIVERABLE_NAMES.has(base) ? 0 : 1;
}

// Deliverables first, then shallower paths (surfaces root-level sdd.md /
// recommendation.json above deep fixture/scaffolding trees), then alpha.
export function sortArtifacts(artifacts: ArtifactRef[]): ArtifactRef[] {
    return [...artifacts].sort((x, y) => {
        const r = artifactRank(x) - artifactRank(y);
        if (r !== 0) return r;
        const dx = x.relPath.split("/").length;
        const dy = y.relPath.split("/").length;
        if (dx !== dy) return dx - dy;
        return x.relPath.localeCompare(y.relPath);
    });
}

export async function walkArtifacts(
    root: string,
    prefix = "",
): Promise<ArtifactRef[]> {
    const out: ArtifactRef[] = [];
    const entries = await fs
        .readdir(root, { withFileTypes: true })
        .catch(() => []);
    for (const e of entries) {
        const full = path.join(root, e.name);
        const rel = prefix ? `${prefix}/${e.name}` : e.name;
        // Skip symlinks: they're harness scaffolding, not deliverables. codex
        // runs symlink every installed skill into `.agents/skills/uipath-*` so
        // the agent can read SKILL.md off disk (no native Skill tool) — 20 per
        // task, pure noise in the list. Skipping also avoids descending a
        // symlinked dir and enumerating its target's contents.
        if (e.isSymbolicLink()) continue;
        if (e.isDirectory()) {
            // Prune whole dirs (.venv, node_modules, ...) via a probe path so
            // the `*/dir/*` patterns alone decide what to descend into — no
            // hardcoded dir names that could drift from the exclude list.
            if (isExcludedArtifact(`${rel}/_`)) continue;
            out.push(...(await walkArtifacts(full, rel)));
        } else {
            if (isExcludedArtifact(rel)) continue;
            const ext = path.extname(e.name).toLowerCase().replace(/^\./, "");
            const stat = await fs.stat(full).catch(() => null);
            out.push({
                relPath: rel,
                kind: ext || "file",
                sizeBytes: stat?.size ?? 0,
            });
        }
    }
    return out;
}

// ---------- Parsers ----------

function extractBalancedJson(
    text: string,
    openIdx: number,
): string | null {
    let depth = 0;
    let inStr = false;
    let escape = false;
    for (let i = openIdx; i < text.length; i++) {
        const ch = text[i];
        if (escape) {
            escape = false;
            continue;
        }
        if (inStr) {
            if (ch === "\\") escape = true;
            else if (ch === '"') inStr = false;
            continue;
        }
        if (ch === '"') inStr = true;
        else if (ch === "{") depth++;
        else if (ch === "}") {
            depth--;
            if (depth === 0) return text.slice(openIdx, i + 1);
        }
    }
    return null;
}

function previewGlobal(val: unknown): string | null {
    if (val == null) return null;
    if (typeof val === "string") return val.length > 200 ? val.slice(0, 200) + "…" : val;
    if (typeof val === "number" || typeof val === "boolean") return String(val);
    try {
        const s = JSON.stringify(val);
        return s.length > 200 ? s.slice(0, 200) + "…" : s;
    } catch {
        return null;
    }
}

function parseFlowDebug(
    criteria: CriterionResult[],
): FlowDebugResult | null {
    const marker = '"Code": "FlowDebug"';
    type Parsed = {
        Code?: string;
        Data?: {
            finalStatus?: string;
            studioWebUrl?: string;
            jobKey?: string;
            elementExecutions?: Array<{
                elementId?: string;
                elementType?: string;
                status?: string;
                startedAt?: string;
                completedAt?: string;
                error?: { message?: string };
            }>;
            variables?: {
                globals?: Record<string, unknown>;
            };
        };
    };
    // FlowDebug payloads are captured verbatim in the details string of the
    // run_command success criterion that invokes `uip maestro flow debug`. Walk
    // backwards from the marker to find the enclosing JSON object, tolerating
    // whatever stdout/stderr prefix the criterion wraps around it.
    for (const c of criteria) {
        const details = c.details ?? "";
        const markerIdx = details.indexOf(marker);
        if (markerIdx === -1) continue;
        for (
            let head = details.lastIndexOf("{", markerIdx);
            head !== -1;
            head = details.lastIndexOf("{", head - 1)
        ) {
            const blob = extractBalancedJson(details, head);
            if (!blob) continue;
            let parsed: Parsed;
            try {
                parsed = JSON.parse(blob) as Parsed;
            } catch {
                continue;
            }
            if (parsed.Code !== "FlowDebug") continue;
            const data = parsed.Data ?? {};
            const globals = data.variables?.globals ?? {};
            const elements: ElementExecution[] = (
                data.elementExecutions ?? []
            ).map((e) => {
                const id = e.elementId ?? "?";
                const outputKey =
                    `${id}.output` in globals
                        ? `${id}.output`
                        : Object.keys(globals).find((k) =>
                            k.startsWith(`${id}.`),
                        );
                const outputVal = outputKey ? globals[outputKey] : null;
                return {
                    elementId: id,
                    elementType: e.elementType ?? null,
                    status: e.status ?? "Unknown",
                    startedAt: e.startedAt ?? null,
                    completedAt: e.completedAt ?? null,
                    errorMessage: e.error?.message ?? null,
                    outputPreview:
                        outputVal != null ? previewGlobal(outputVal) : null,
                };
            });
            return {
                finalStatus: data.finalStatus ?? null,
                studioWebUrl: data.studioWebUrl ?? null,
                jobKey: data.jobKey ?? null,
                elements,
            };
        }
    }
    return null;
}

interface CommandEntry {
    tool_name?: string;
    tool_id?: string;
    parameters?: Record<string, unknown>;
    duration_ms?: number;
    result_status?: string;
    result_summary?: unknown;
    error_message?: string | null;
}

interface ContentBlockEntry {
    block_type?: "thinking" | "tool_use" | "text";
    text?: string | null;
    thinking?: string | null;
    tool_use_id?: string | null;
    is_error?: boolean;
}

interface MessageEntry {
    role?: string;
    started_at?: string | null;
    completed_at?: string | null;
    generation_duration_ms?: number | null;
    content_blocks?: ContentBlockEntry[];
    // Anthropic API message_id. The Claude Code CLI splits one API response
    // into per-block-kind events that share this id; we collapse by id when
    // present, falling back to a wall-clock gap heuristic for older runs that
    // didn't record it.
    message_id?: string | null;
    // The Task tool_use_id that spawned this message's sub-agent, or null for
    // the main thread. KEY ABSENT (undefined) on runs that predate branch
    // capture — distinct from an explicit null. Used to model the cache cascade
    // as a tree; its absence disables the cost simulator (we can't tell whether
    // sub-agents ran, so a flat cascade would be wrong).
    parent_tool_use_id?: string | null;
    // Per-message token usage (recorded since coder_eval #336). Absent on
    // legacy runs. cache_* keys here are the message-record names, distinct
    // from the iteration token_usage's cache_*_input_tokens.
    input_tokens?: number | null;
    output_tokens?: number | null;
    cache_creation_tokens?: number | null;
    cache_read_tokens?: number | null;
    reasoning_tokens?: number | null;
    model?: string | null;
}

export interface TurnEntry {
    commands?: CommandEntry[];
    messages?: MessageEntry[];
    token_usage?: TokenUsageEntry | null;
    result_summary?: {
        result?: string | null;
        stop_reason?: string | null;
    } | null;
    sub_agent_usage?: SubAgentUsageEntry[] | null;
}

interface SubAgentUsageEntry {
    tool_use_id?: string | null;
    input_tokens?: number | null;
    output_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
    cache_read_input_tokens?: number | null;
    total_tokens?: number | null;
    tool_uses?: number | null;
    duration_ms?: number | null;
    status?: string | null;
}

interface TokenUsageEntry {
    input_tokens?: number | null;
    output_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
    cache_read_input_tokens?: number | null;
    total_cost_usd?: number | null;
}

// Token counts summed across all iterations of a task. Optional so legacy
// task.json files without per-iteration token_usage just expose zeros.
export interface TokenTotals {
    input: number;
    output: number;
    cacheCreation: number;
    cacheRead: number;
    total: number;
}

function summarizeCommand(cmd: CommandEntry): string {
    const p = cmd.parameters ?? {};
    // Prefer the first human-readable field the tool provides, in priority
    // order. Falls back to a stringified params object if none match.
    const keys = [
        "description",
        "command",
        "file_path",
        "skill",
        "query",
        "pattern",
    ];
    for (const k of keys) {
        const v = p[k];
        if (typeof v === "string" && v.length > 0) {
            const flat = v.replace(/[\n\t]+/g, " ").trim();
            return flat.length > 140 ? flat.slice(0, 137) + "…" : flat;
        }
    }
    let s: string;
    try {
        s = JSON.stringify(p);
    } catch {
        return "";
    }
    return s.length > 140 ? s.slice(0, 137) + "…" : s;
}

// Cap message-level previews to keep payloads sane on long-running tasks.
// Result previews are stored short anyway (the raw task.json truncates them
// at ~200 chars for most non-Bash tools); we just need a deterministic ceiling.
const RESULT_PREVIEW_CAP = 280;
const TEXT_PREVIEW_CAP = 600;
const THINKING_PREVIEW_CAP = 800;

// Pick the operative arg the agent actually ran with. For Bash that's the
// command string; for Read/Write/Edit it's file_path; for Skill it's the skill
// id; for Grep it's pattern; for WebFetch it's url; etc. Falls through to the
// first non-empty string param if no known key matched.
function pickArgText(params: Record<string, unknown>): string | null {
    const keys = [
        "command",
        "file_path",
        "filePath",
        "path",
        "url",
        "skill",
        "pattern",
        "query",
        // Agent (sub-agent) tool: the operative input is the delegation prompt,
        // not the short `description`. Without this the row showed only the
        // one-line description, so a 1000+ char prompt looked like a tiny input
        // next to a large output-token count.
        "prompt",
    ];
    for (const k of keys) {
        const v = params[k];
        if (typeof v === "string" && v.length > 0) return v;
    }
    for (const v of Object.values(params)) {
        if (typeof v === "string" && v.length > 0) return v;
    }
    return null;
}

function pickDescription(params: Record<string, unknown>): string | null {
    const v = params["description"];
    return typeof v === "string" && v.length > 0 ? v : null;
}

function previewString(value: unknown, cap: number): string | null {
    if (value == null) return null;
    let s: string;
    if (typeof value === "string") {
        s = value;
    } else {
        try {
            s = JSON.stringify(value);
        } catch {
            return null;
        }
    }
    s = s.replace(/[\r]+/g, "");
    if (s.length > cap) s = s.slice(0, cap - 1) + "…";
    return s.length > 0 ? s : null;
}

// Gap between one assistant message's completed_at and the next message's
// started_at that we treat as "same model emission". coder_eval records each
// content-block kind (thinking / tool_use / text) as its own MessageEntry,
// so a single conceptual turn often spans 2-3 entries with gap≈0. Tool
// execution sits *between* turns and adds 100ms+, so 100ms is a safe split
// threshold. Anything below is grouped into one logical turn; this is also
// how parallel tool_use blocks (whether one MessageEntry with N tool_use
// blocks, or N MessageEntries emitted instantaneously) get folded together.
const SAME_EMISSION_GAP_MS = 100;

function tsMs(s: string | null | undefined): number | null {
    if (!s) return null;
    const t = Date.parse(s);
    return Number.isNaN(t) ? null : t;
}

// ~4 chars/token is the common rough estimate for English/code. The exact
// constant doesn't matter for *splitting* time across parallel tools (it
// cancels in the ratio), but using a real-ish number keeps the helper useful
// if anyone wants a token estimate independently.
const CHARS_PER_TOKEN = 4;

export function approxTokens(params: Record<string, unknown> | null | undefined): number {
    if (!params) return 0;
    try {
        const s = JSON.stringify(params);
        return Math.ceil(s.length / CHARS_PER_TOKEN);
    } catch {
        return 0;
    }
}

export function parseMessages(turns: TurnEntry[]): MessageEvent[] {
    const out: MessageEvent[] = [];
    let order = 0;
    for (const turn of turns) {
        // Resolve tool_use_id -> CommandEntry per iteration (the id is locally
        // unique). Used to pull execution time + result preview onto tool_use
        // blocks.
        const byToolUseId = new Map<string, CommandEntry>();
        for (const cmd of turn.commands ?? []) {
            if (cmd.tool_id) byToolUseId.set(cmd.tool_id, cmd);
        }

        // First pass: collect assistant messages with parsed block content.
        type Raw = {
            startedAt: string | null;
            completedAt: string | null;
            startMs: number | null;
            endMs: number | null;
            generationMs: number | null;
            messageId: string | null;
            // null = main thread, string = sub-agent branch, undefined = run
            // didn't record branch info (legacy).
            parentToolUseId: string | null | undefined;
            blockTypes: ("thinking" | "tool_use" | "text")[];
            thinkingParts: string[];
            textParts: string[];
            toolUses: MessageToolUse[];
            toolTokenProxies: number[];
            inputTokens: number | null;
            outputTokens: number | null;
            cacheWriteTokens: number | null;
            cacheReadTokens: number | null;
            reasoningTokens: number | null;
            model: string | null;
        };
        const raws: Raw[] = [];
        for (const msg of turn.messages ?? []) {
            if (msg.role !== "assistant") continue;
            const r: Raw = {
                startedAt: msg.started_at ?? null,
                completedAt: msg.completed_at ?? null,
                startMs: tsMs(msg.started_at),
                endMs: tsMs(msg.completed_at),
                generationMs:
                    typeof msg.generation_duration_ms === "number"
                        ? msg.generation_duration_ms
                        : null,
                messageId: typeof msg.message_id === "string" ? msg.message_id : null,
                // Preserve the absent/null distinction: undefined when the run
                // never wrote the key (legacy), null when explicitly main thread.
                parentToolUseId:
                    msg.parent_tool_use_id === undefined
                        ? undefined
                        : typeof msg.parent_tool_use_id === "string"
                          ? msg.parent_tool_use_id
                          : null,
                blockTypes: [],
                thinkingParts: [],
                textParts: [],
                toolUses: [],
                toolTokenProxies: [],
                inputTokens:
                    typeof msg.input_tokens === "number" ? msg.input_tokens : null,
                outputTokens:
                    typeof msg.output_tokens === "number" ? msg.output_tokens : null,
                cacheWriteTokens:
                    typeof msg.cache_creation_tokens === "number"
                        ? msg.cache_creation_tokens
                        : null,
                cacheReadTokens:
                    typeof msg.cache_read_tokens === "number"
                        ? msg.cache_read_tokens
                        : null,
                reasoningTokens:
                    typeof msg.reasoning_tokens === "number"
                        ? msg.reasoning_tokens
                        : null,
                model: typeof msg.model === "string" ? msg.model : null,
            };
            for (const b of msg.content_blocks ?? []) {
                if (b.block_type === "thinking") {
                    r.blockTypes.push("thinking");
                    if (b.thinking) r.thinkingParts.push(b.thinking);
                } else if (b.block_type === "text") {
                    r.blockTypes.push("text");
                    if (b.text) r.textParts.push(b.text);
                } else if (b.block_type === "tool_use") {
                    r.blockTypes.push("tool_use");
                    const id = b.tool_use_id ?? null;
                    const cmd = id ? byToolUseId.get(id) : undefined;
                    const params = cmd?.parameters ?? {};
                    const argText = pickArgText(params);
                    const description = pickDescription(params);
                    // Crude token proxy: serialized argument byte length. Used
                    // below to weight per-tool generation time across parallel
                    // tool_use blocks emitted in one raw.
                    const tokenProxy = approxTokens(params);
                    r.toolTokenProxies.push(tokenProxy);
                    r.toolUses.push({
                        toolName: cmd?.tool_name ?? "unknown",
                        toolUseId: id,
                        summary: cmd ? summarizeCommand(cmd) : "",
                        argText: previewString(argText, 400),
                        description:
                            description && description !== argText
                                ? previewString(description, 200)
                                : null,
                        // genMs set below after we know how many tool_uses share this raw
                        genMs: null,
                        durationMs:
                            typeof cmd?.duration_ms === "number"
                                ? cmd.duration_ms
                                : null,
                        isError:
                            b.is_error === true ||
                            (cmd?.result_status != null &&
                                cmd.result_status !== "success"),
                        resultPreview: previewString(
                            cmd?.result_summary ?? cmd?.error_message ?? null,
                            RESULT_PREVIEW_CAP,
                        ),
                        outputTokens: null,
                    });
                }
            }
            // Split the raw's generation time across its tool_use blocks,
            // weighted by an approximate token count (param payload size).
            // The SDK only reports one generation_duration_ms per raw, so for
            // parallel tool_uses we attribute proportional to argument size —
            // larger calls plausibly cost more output tokens to generate. Falls
            // back to even-split when all proxies are zero.
            if (r.generationMs != null && r.toolUses.length > 0) {
                const proxies = r.toolTokenProxies;
                const total = proxies.reduce((a, b) => a + b, 0);
                for (let i = 0; i < r.toolUses.length; i++) {
                    const weight =
                        total > 0 ? proxies[i] / total : 1 / r.toolUses.length;
                    r.toolUses[i].genMs = r.generationMs * weight;
                }
            }
            // Per-tool output tokens. The agent records output_tokens per
            // emission, so a tool emission's output_tokens belongs to its
            // tool_use block(s) directly — no gen-time guesswork. Only when a
            // single emission carries multiple parallel tool_uses do we split
            // it (by arg-size proxy, exact remainder on the last tool).
            if (r.outputTokens != null && r.toolUses.length > 0) {
                const proxies = r.toolTokenProxies;
                const total = proxies.reduce((a, b) => a + b, 0);
                let assigned = 0;
                for (let i = 0; i < r.toolUses.length; i++) {
                    if (i === r.toolUses.length - 1) {
                        r.toolUses[i].outputTokens = r.outputTokens - assigned;
                    } else {
                        const weight =
                            total > 0 ? proxies[i] / total : 1 / r.toolUses.length;
                        const share = Math.round(r.outputTokens * weight);
                        r.toolUses[i].outputTokens = share;
                        assigned += share;
                    }
                }
            }
            raws.push(r);
        }

        // Second pass: collapse consecutive raws that share a model emission
        // (tiny gap between prev.end and next.start). Parallel tool_use lives
        // entirely inside one emission, so it ends up in one MessageEvent.
        let group: Raw[] = [];
        const flush = () => {
            if (group.length === 0) return;
            const head = group[0];
            const tail = group[group.length - 1];
            const blockTypes: MessageEvent["blockTypes"] = [];
            const thinkingParts: string[] = [];
            const textParts: string[] = [];
            const toolUses: MessageToolUse[] = [];
            let genSum = 0;
            let haveGen = false;
            // Each raw is one content-block emission (see SAME_EMISSION_GAP_MS
            // comment), so its generation_duration_ms attaches to whichever
            // block kind it carries.
            let thinkSum = 0;
            let haveThink = false;
            let textSum = 0;
            let haveText = false;
            let toolGenSum = 0;
            let haveToolGen = false;
            let inputTokSum = 0;
            let haveInputTok = false;
            let outputTokSum = 0;
            let haveOutputTok = false;
            let cacheWriteSum = 0;
            let haveCacheWrite = false;
            let cacheReadSum = 0;
            let haveCacheRead = false;
            let reasoningSum = 0;
            let haveReasoning = false;
            // Real per-emission output attributed to thinking: the agent
            // distributes a call's output_tokens across its block-emissions by
            // content length, so a thinking-only emission's output_tokens IS
            // the thinking share (reasoning_tokens is ~always 0 from the SDK,
            // so we don't rely on it). Mirrors the gen-time attribution below.
            let thinkingOutSum = 0;
            let haveThinkingOut = false;
            // Likewise for text: a text emission's output_tokens is its share.
            let textOutSum = 0;
            let haveTextOut = false;
            // First non-null model id in the group — consumed by the
            // cascade-aware thinking-cost simulator to price each message.
            let model: string | null = null;
            for (const r of group) {
                blockTypes.push(...r.blockTypes);
                thinkingParts.push(...r.thinkingParts);
                textParts.push(...r.textParts);
                toolUses.push(...r.toolUses);
                if (model == null && r.model != null) model = r.model;
                if (r.generationMs != null) {
                    genSum += r.generationMs;
                    haveGen = true;
                    if (r.blockTypes.includes("thinking")) {
                        thinkSum += r.generationMs;
                        haveThink = true;
                    } else if (r.blockTypes.includes("tool_use")) {
                        toolGenSum += r.generationMs;
                        haveToolGen = true;
                    } else if (r.blockTypes.includes("text")) {
                        textSum += r.generationMs;
                        haveText = true;
                    }
                }
                if (r.inputTokens != null) {
                    inputTokSum += r.inputTokens;
                    haveInputTok = true;
                }
                if (r.outputTokens != null) {
                    outputTokSum += r.outputTokens;
                    haveOutputTok = true;
                    // Attribute the emission's output to its block kind (each
                    // raw is one kind; priority mirrors the gen-time split).
                    // Tool output is attached per-tool in the first pass.
                    if (r.blockTypes.includes("thinking")) {
                        thinkingOutSum += r.outputTokens;
                        haveThinkingOut = true;
                    } else if (r.blockTypes.includes("text")) {
                        textOutSum += r.outputTokens;
                        haveTextOut = true;
                    }
                }
                if (r.cacheWriteTokens != null) {
                    cacheWriteSum += r.cacheWriteTokens;
                    haveCacheWrite = true;
                }
                if (r.cacheReadTokens != null) {
                    cacheReadSum += r.cacheReadTokens;
                    haveCacheRead = true;
                }
                if (r.reasoningTokens != null) {
                    reasoningSum += r.reasoningTokens;
                    haveReasoning = true;
                }
            }
            // Per-block output comes straight from each emission's recorded
            // output_tokens (thinking + text here, tools in the first pass) —
            // the agent already split the call total across blocks by content
            // length, so there's no re-approximation to do. These sum to the
            // group's outputTokens.
            const textOutputTokens = haveTextOut ? textOutSum : null;
            out.push({
                index: ++order,
                role: "assistant",
                startedAt: head.startedAt,
                completedAt: tail.completedAt,
                generationMs: haveGen ? genSum : null,
                thinkingMs: haveThink ? thinkSum : null,
                textMs: haveText ? textSum : null,
                toolGenMs: haveToolGen ? toolGenSum : null,
                blockTypes,
                thinkingText: previewString(
                    thinkingParts.join("\n").trim(),
                    THINKING_PREVIEW_CAP,
                ),
                text: previewString(textParts.join("\n").trim(), TEXT_PREVIEW_CAP),
                toolUses,
                inputTokens: haveInputTok ? inputTokSum : null,
                outputTokens: haveOutputTok ? outputTokSum : null,
                cacheWriteTokens: haveCacheWrite ? cacheWriteSum : null,
                cacheReadTokens: haveCacheRead ? cacheReadSum : null,
                // All raws in a group are one emission → one branch; head is
                // representative.
                parentToolUseId: head.parentToolUseId,
                reasoningTokens: haveReasoning ? reasoningSum : null,
                thinkingOutputTokens: haveThinkingOut ? thinkingOutSum : null,
                textOutputTokens,
                model,
                costUsd: messageCostUsd({
                    model,
                    inputTokens: haveInputTok ? inputTokSum : null,
                    outputTokens: haveOutputTok ? outputTokSum : null,
                    cacheWriteTokens: haveCacheWrite ? cacheWriteSum : null,
                    cacheReadTokens: haveCacheRead ? cacheReadSum : null,
                }),
            });
            group = [];
        };
        for (const r of raws) {
            if (group.length === 0) {
                group.push(r);
                continue;
            }
            const prev = group[group.length - 1];
            // Prefer message_id when both raws have one — that's the
            // authoritative signal that the CLI split a single API response.
            // Fall back to the wall-clock gap heuristic for older runs that
            // didn't record message_id.
            let sameEmission: boolean;
            if (prev.messageId != null && r.messageId != null) {
                sameEmission = prev.messageId === r.messageId;
            } else {
                const gap =
                    prev.endMs != null && r.startMs != null
                        ? r.startMs - prev.endMs
                        : Number.POSITIVE_INFINITY;
                sameEmission = gap <= SAME_EMISSION_GAP_MS;
            }
            if (sameEmission) {
                group.push(r);
            } else {
                flush();
                group.push(r);
            }
        }
        flush();
    }
    return out;
}

function parseToolCalls(turns: TurnEntry[], max = 200): ToolCall[] {
    const out: ToolCall[] = [];
    let i = 0;
    for (const turn of turns) {
        for (const cmd of turn.commands ?? []) {
            if (!cmd.tool_name) continue;
            out.push({
                index: ++i,
                tool: cmd.tool_name,
                summary: summarizeCommand(cmd),
            });
            if (out.length >= max) return out;
        }
    }
    return out;
}

// ---------- Task detail ----------

// Runs uploaded before the replicate-index layout (<taskDir>/task.json) and
// after (<taskDir>/00/task.json) coexist in the blob store. Prefer the
// nested shape; fall back to flat when probing nested task.json fails so
// legacy runs keep rendering.
async function resolveTaskContentDir(taskDir: string): Promise<string> {
    const nested = path.join(taskDir, "00");
    try {
        await fs.access(path.join(nested, "task.json"));
        return nested;
    } catch {
        return taskDir;
    }
}

export async function readTaskDetail(
    runId: string,
    taskId: string,
): Promise<TaskDetail | null> {
    await ensureTaskDir(runId, taskId, RUNS_DIR);

    const data = await readRunJson(runId);
    const rawTask = data?.task_results?.find((t) => t.task_id === taskId);
    if (!rawTask) return null;
    const row = toTaskRow(rawTask);

    const taskDir = path.join(RUNS_DIR, runId, "default", taskId);
    const contentDir = await resolveTaskContentDir(taskDir);
    const task = await readJson<{
        final_status?: string;
        error_message?: string;
        task_description?: string;
        task_config?: {
            resolved?: {
                description?: string;
                initial_prompt?: string;
            };
        };
        success_criteria_results?: Array<{
            criterion_type?: string;
            description?: string;
            score?: number;
            details?: string;
            error?: string | null;
        }>;
        iterations?: TurnEntry[];
    }>(path.join(contentDir, "task.json"));

    const criteria: CriterionResult[] = (
        task?.success_criteria_results ?? []
    ).map((c) => ({
        criterionType: c.criterion_type ?? null,
        description: c.description ?? null,
        score: c.score ?? null,
        details: c.details ?? null,
        error: c.error ?? null,
    }));

    const artifactRoot = path.join(contentDir, "artifacts");
    // relPath is stored relative to the run root so the /api/file route can
    // resolve it against RUNS_DIR/<runId> without needing to know the task
    // subdir. New layout yields `default/<task_id>/00/artifacts/...`; flat
    // layout yields `default/<task_id>/artifacts/...`. `resolveSafePath`
    // validates parts[0] and parts[1], so both shapes pass the check.
    const artifactPrefix = path.relative(
        path.join(RUNS_DIR, runId),
        artifactRoot,
    );
    const artifacts = sortArtifacts(
        await walkArtifacts(artifactRoot, artifactPrefix),
    );

    const flowDebug = parseFlowDebug(criteria);
    const toolCalls = parseToolCalls(task?.iterations ?? []);
    const messages = parseMessages(task?.iterations ?? []);
    // The per-message stream is the authoritative cumulative bill and is
    // preferred whenever it carries token data. Iteration token_usage comes
    // from the SDK ResultMessage snapshot, which is NOT cumulative for
    // cache/input tokens — on a multi-call run it under-reports
    // cache_read_input_tokens by the re-read cascade (each later call re-reads
    // the growing transcript and is billed for it), often 2-3x low. Anthropic
    // bills cache reads per request, so the true total is the sum of each
    // call's usage. parseMessages already collapses per message_id, so this sum
    // doesn't double-count the CLI's repeated usage dicts. Fall back to the
    // iteration aggregate only for runs that never recorded per-message tokens.
    const tokens = selectTokenTotals(messages, task?.iterations ?? []);

    // Per-sub-agent token breakdown keyed by the spawning Agent tool_use_id.
    // Components summed in case one Agent call is reported across multiple
    // entries; total falls back to the component sum when not recorded.
    const subAgentUsageByToolId: Record<string, SubAgentTotals> = {};
    for (const it of task?.iterations ?? []) {
        for (const sa of it.sub_agent_usage ?? []) {
            const id = sa.tool_use_id;
            if (typeof id !== "string") continue;
            const input = sa.input_tokens ?? 0;
            const output = sa.output_tokens ?? 0;
            const cacheCreation = sa.cache_creation_input_tokens ?? 0;
            const cacheRead = sa.cache_read_input_tokens ?? 0;
            const total =
                sa.total_tokens ??
                input + output + cacheCreation + cacheRead;
            const prev = subAgentUsageByToolId[id];
            subAgentUsageByToolId[id] = {
                total: (prev?.total ?? 0) + total,
                input: (prev?.input ?? 0) + input,
                output: (prev?.output ?? 0) + output,
                cacheCreation: (prev?.cacheCreation ?? 0) + cacheCreation,
                cacheRead: (prev?.cacheRead ?? 0) + cacheRead,
            };
        }
    }

    const taskDescription =
        task?.task_config?.resolved?.initial_prompt ??
        task?.task_config?.resolved?.description ??
        task?.task_description ??
        null;

    // The trailing text-only assistant message lives on the last turn's
    // ResultMessage. Walk from the end so partial/crashed turns earlier
    // in the run don't shadow it.
    let finalAssistantText: string | null = null;
    for (let i = (task?.iterations?.length ?? 0) - 1; i >= 0; i--) {
        const r = task?.iterations?.[i]?.result_summary?.result;
        if (typeof r === "string" && r.length > 0) {
            finalAssistantText = r;
            break;
        }
    }

    return {
        ...row,
        // task.json is the richer source: it carries the trailing text
        // even on legacy run.json files that predate has_final_reply.
        // Overriding row.hasFinalReply here keeps detail TURNS, Turn
        // timeline header, and the reply row in lockstep.
        hasFinalReply: finalAssistantText != null,
        runId,
        finalStatus: task?.final_status ?? null,
        errorMessage: task?.error_message ?? null,
        taskDescription,
        criteria,
        artifacts,
        flowDebug,
        toolCalls,
        finalAssistantText,
        messages,
        tokens,
        subAgentUsageByToolId,
    };
}

// Pick the run's authoritative cumulative token totals. The per-message stream
// run-level token totals, picking the more complete of two sources:
//   - iteration token_usage: on current runs this is built from the SDK
//     ResultMessage `model_usage` (cumulative per-model billing, reconciles to
//     total_cost_usd, and INCLUDES sub-agent cache-creation/input). On legacy
//     runs it is the old ResultMessage `usage` snapshot, which under-reports the
//     cache-read cascade ~2-3x.
//   - per-message stream sum: deduped per message_id; captures the cache-read
//     cascade but UNDER-reports sub-agent tokens (those emissions aren't fully
//     bubbled into the stream).
// Neither dominates across both run vintages, so pick the larger total — the
// more complete figure. New runs → iteration (model_usage) wins; legacy runs →
// per-message wins over the stale snapshot.
export function selectTokenTotals(
    messages: MessageEvent[],
    turns: TurnEntry[],
): TokenTotals {
    const fromMessages = sumMessageTokens(messages);
    const fromIterations = sumTokenTotals(turns);
    return fromIterations.total >= fromMessages.total
        ? fromIterations
        : fromMessages;
}

// Token totals from the iteration token_usage aggregate (model_usage-derived on
// current runs; the ResultMessage snapshot on legacy runs).
function sumTokenTotals(turns: TurnEntry[]): TokenTotals {
    let input = 0;
    let output = 0;
    let cacheCreation = 0;
    let cacheRead = 0;
    for (const t of turns) {
        const tu = t.token_usage;
        if (!tu) continue;
        input += tu.input_tokens ?? 0;
        output += tu.output_tokens ?? 0;
        cacheCreation += tu.cache_creation_input_tokens ?? 0;
        cacheRead += tu.cache_read_input_tokens ?? 0;
    }
    return {
        input,
        output,
        cacheCreation,
        cacheRead,
        total: input + output + cacheCreation + cacheRead,
    };
}

// Token totals from the collapsed per-message stream. MessageEvent tokens are
// already summed per message_id group (see parseMessages), so they don't
// double-count the CLI's repeated usage dicts. Captures the per-request
// cache-read cascade but under-reports sub-agent tokens (their emissions aren't
// fully bubbled up) — one of the two inputs selectTokenTotals chooses between.
function sumMessageTokens(messages: MessageEvent[]): TokenTotals {
    let input = 0;
    let output = 0;
    let cacheCreation = 0;
    let cacheRead = 0;
    for (const m of messages) {
        input += m.inputTokens ?? 0;
        output += m.outputTokens ?? 0;
        cacheCreation += m.cacheWriteTokens ?? 0;
        cacheRead += m.cacheReadTokens ?? 0;
    }
    return {
        input,
        output,
        cacheCreation,
        cacheRead,
        total: input + output + cacheCreation + cacheRead,
    };
}

export async function readRunAnalysis(runId: string): Promise<string | null> {
    await ensureRunAnalysis(runId, RUNS_DIR);
    return fs
        .readFile(path.join(RUNS_DIR, runId, "analysis.md"), "utf-8")
        .catch(() => null);
}

// ---------- run metadata (meta.json sidecar) ----------

// Optional, user-supplied at upload time. Null on pipeline runs and any run
// uploaded before the feature — every consumer must treat null as "no
// metadata" (title falls back to the run id, adhoc defaults false).
export interface RunMeta {
    title: string | null;
    description: string | null;
    adhoc: boolean;
}

interface RawRunMeta {
    title?: string | null;
    description?: string | null;
    adhoc?: boolean;
}

export async function readRunMeta(runId: string): Promise<RunMeta | null> {
    await ensureRunMeta(runId, RUNS_DIR);
    const raw = await readJson<RawRunMeta>(
        path.join(RUNS_DIR, runId, "meta.json"),
    );
    if (!raw) return null;
    return {
        title: raw.title ?? null,
        description: raw.description ?? null,
        adhoc: raw.adhoc === true,
    };
}

export async function readLogTail(
    runId: string,
    taskId: string,
    maxBytes = 200_000,
): Promise<string> {
    await ensureTaskDir(runId, taskId, RUNS_DIR);
    const taskDir = path.join(RUNS_DIR, runId, "default", taskId);
    const contentDir = await resolveTaskContentDir(taskDir);
    const logPath = path.join(contentDir, "task.log");
    const raw = await fs.readFile(logPath, "utf-8").catch(() => "");
    if (raw.length <= maxBytes) return raw;
    return `… (truncated, showing last ${maxBytes} bytes)\n\n${raw.slice(-maxBytes)}`;
}

// Collect every file under a task's folder (`default/<taskId>/`) for the
// download-as-zip button on the task page. Reuses walkArtifacts so the same
// noise filter (`.venv`, `node_modules`, `*.pyc`, lockfiles, secrets) and
// symlink skip that drive the Artifacts list also shape the zip — plus
// task.json / task.log at the task root, which aren't excluded by any pattern.
// Returns null for an invalid id or a missing/empty task dir.
export async function collectTaskFiles(
    runId: string,
    taskId: string,
): Promise<{ relPath: string; abs: string }[] | null> {
    if (!isValidId(runId) || !isValidTaskId(taskId)) return null;
    await ensureTaskDir(runId, taskId, RUNS_DIR);
    const taskDir = path.join(RUNS_DIR, runId, "default", taskId);
    const refs = await walkArtifacts(taskDir);
    if (refs.length === 0) return null;
    return refs.map((r) => ({ relPath: r.relPath, abs: path.join(taskDir, r.relPath) }));
}

// Collect every file under a whole run (`<runId>/`) for the download-as-zip
// button on the run page. Same noise filter / symlink skip as collectTaskFiles,
// applied across all task subdirs plus run-level files (run.json, analysis.md,
// meta.json, …). Returns null for an invalid id or a missing/empty run dir.
export async function collectRunFiles(
    runId: string,
): Promise<{ relPath: string; abs: string }[] | null> {
    if (!isValidId(runId)) return null;
    await ensureRunDir(runId, RUNS_DIR);
    const runDir = path.join(RUNS_DIR, runId);
    const refs = await walkArtifacts(runDir);
    if (refs.length === 0) return null;
    return refs.map((r) => ({ relPath: r.relPath, abs: path.join(runDir, r.relPath) }));
}

export async function resolveSafePath(
    runId: string,
    relPath: string,
): Promise<string | null> {
    if (!isValidId(runId)) return null;
    // Artifact URLs embed the task subdir in relPath
    // (`default/<task-id>/artifacts/...`) — extract it so the narrow fetch
    // hits the right blobs without pulling the whole run.
    const parts = relPath.split("/");
    if (parts[0] === "default" && parts[1]) {
        if (!isValidId(parts[1])) return null;
        await ensureTaskDir(runId, parts[1], RUNS_DIR);
    } else {
        await ensureRunSummary(runId, RUNS_DIR);
    }
    const base = path.join(RUNS_DIR, runId);
    const baseReal = await fs.realpath(base).catch(() => null);
    if (!baseReal) return null;
    const candidate = path.resolve(baseReal, relPath);
    if (
        candidate !== baseReal &&
        !candidate.startsWith(baseReal + path.sep)
    ) {
        return null;
    }
    // Canonicalize the target to catch symlinks under the run dir that point
    // outside it. If the target doesn't exist yet, fall back to the lexical
    // candidate so the caller can distinguish "outside dir" from "not found".
    const candidateReal = await fs.realpath(candidate).catch(() => null);
    if (candidateReal == null) return candidate;
    if (
        candidateReal !== baseReal &&
        !candidateReal.startsWith(baseReal + path.sep)
    ) {
        return null;
    }
    return candidateReal;
}

// Delete a run's locally-cached blob copy under `root` so the next view
// re-downloads it from storage. `force: true` makes a never-cached run a
// harmless no-op. Returns false (deleting nothing) for an unsafe id — note
// isValidId still admits "." and ".." (dots are word-ish), so require the
// resolved target to be a strict child of `root` before rm can run, or a "."
// id would nuke the cache root and ".." its parent.
export async function clearRunCacheDir(
    root: string,
    id: string,
): Promise<boolean> {
    if (!isValidId(id)) return false;
    const base = path.resolve(root);
    const dir = path.resolve(base, id);
    if (dir === base || !dir.startsWith(base + path.sep)) return false;
    await fs.rm(dir, { recursive: true, force: true });
    return true;
}
