import { promises as fs } from "node:fs";
import path from "node:path";
import {
    LOCAL_RUNS_DIR,
    ensureRunAnalysis,
    ensureRunSummary,
    ensureTaskDir,
    isValidId,
    listRunIdsRemote,
} from "./blob";

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

export interface ArtifactRef {
    relPath: string;
    kind: "flow" | "uipx" | "uiproj" | "other";
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
}

// ---------- run.json schema ----------

interface RawTaskResult {
    task_id?: string;
    status?: string;
    weighted_score?: number;
    duration?: number;
    total_cost_usd?: number;
    actual_commands?: number;
    tags?: string[];
    // Source YAML path. Persisted by coder-eval starting with the
    // task_path PR; absent on older runs (deriveSkill falls back to tags).
    task_path?: string | null;
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
    environment_info?: Record<string, string | number | null>;
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

function extractComponentShas(
    env: Record<string, string | number | null> | undefined,
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

function toTaskRow(t: RawTaskResult): TaskResultSummary {
    const tags = t.tags ?? [];
    return {
        taskId: t.task_id ?? "",
        status: t.status ?? null,
        weightedScore: t.weighted_score ?? null,
        durationSeconds: t.duration ?? null,
        totalCostUsd: t.total_cost_usd ?? null,
        actualCommands: t.actual_commands ?? null,
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
    return (data.task_results ?? [])
        .filter((t) => t.task_id)
        .map(toTaskRow);
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

async function walkArtifacts(
    root: string,
    prefix = "",
): Promise<ArtifactRef[]> {
    const out: ArtifactRef[] = [];
    const entries = await fs
        .readdir(root, { withFileTypes: true })
        .catch(() => []);
    for (const e of entries) {
        if (e.name === ".venv" || e.name === "node_modules") continue;
        const full = path.join(root, e.name);
        const rel = prefix ? `${prefix}/${e.name}` : e.name;
        if (e.isDirectory()) {
            out.push(...(await walkArtifacts(full, rel)));
        } else {
            const ext = path.extname(e.name).toLowerCase();
            let kind: ArtifactRef["kind"] = "other";
            if (ext === ".flow") kind = "flow";
            else if (ext === ".uipx") kind = "uipx";
            else if (ext === ".uiproj") kind = "uiproj";
            if (kind === "other") continue;
            const stat = await fs.stat(full).catch(() => null);
            out.push({
                relPath: rel,
                kind,
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
    parameters?: Record<string, unknown>;
}

interface TurnEntry {
    commands?: CommandEntry[];
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
        turns?: TurnEntry[];
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
    const artifacts = await walkArtifacts(artifactRoot, artifactPrefix);

    const flowDebug = parseFlowDebug(criteria);
    const toolCalls = parseToolCalls(task?.turns ?? []);

    const taskDescription =
        task?.task_config?.resolved?.initial_prompt ??
        task?.task_config?.resolved?.description ??
        task?.task_description ??
        null;

    return {
        ...row,
        runId,
        finalStatus: task?.final_status ?? null,
        errorMessage: task?.error_message ?? null,
        taskDescription,
        criteria,
        artifacts,
        flowDebug,
        toolCalls,
    };
}

export async function readRunAnalysis(runId: string): Promise<string | null> {
    await ensureRunAnalysis(runId, RUNS_DIR);
    return fs
        .readFile(path.join(RUNS_DIR, runId, "analysis.md"), "utf-8")
        .catch(() => null);
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
