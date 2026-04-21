import { promises as fs } from "node:fs";
import path from "node:path";

export const RUNS_DIR = process.env.EVALBOARD_RUNS_DIR
    ? path.resolve(process.env.EVALBOARD_RUNS_DIR)
    : path.resolve(process.cwd(), "..", "runs");

export interface RunSummary {
    id: string;
    taskId: string | null;
    status: string | null;
    weightedScore: number | null;
    durationSeconds: number | null;
    totalCostUsd: number | null;
    actualCommands: number | null;
    tags: string[];
    startTime: string | null;
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

export interface RunDetail extends RunSummary {
    finalStatus: string | null;
    errorMessage: string | null;
    taskDescription: string | null;
    criteria: CriterionResult[];
    artifacts: ArtifactRef[];
    logPath: string | null;
    taskJsonPath: string | null;
    taskSubdir: string | null;
    flowDebug: FlowDebugResult | null;
    toolCalls: ToolCall[];
}

export interface ArtifactRef {
    relPath: string;
    kind: "flow" | "uipx" | "uiproj" | "other";
    sizeBytes: number;
}

export async function listRunIds(): Promise<string[]> {
    const entries = await fs.readdir(RUNS_DIR, { withFileTypes: true });
    return entries
        .filter((e) => e.isDirectory() && e.name !== "latest")
        .map((e) => e.name)
        .sort()
        .reverse();
}

async function readJson<T>(p: string): Promise<T | null> {
    try {
        const raw = await fs.readFile(p, "utf-8");
        return JSON.parse(raw) as T;
    } catch {
        return null;
    }
}

export async function readRunSummary(id: string): Promise<RunSummary | null> {
    const runJsonPath = path.join(RUNS_DIR, id, "run.json");
    const data = await readJson<{
        start_time?: string;
        task_results?: Array<{
            task_id?: string;
            status?: string;
            weighted_score?: number;
            duration?: number;
            total_cost_usd?: number;
            actual_commands?: number;
            tags?: string[];
        }>;
    }>(runJsonPath);
    if (!data) return null;
    const first = data.task_results?.[0] ?? {};
    return {
        id,
        taskId: first.task_id ?? null,
        status: first.status ?? null,
        weightedScore: first.weighted_score ?? null,
        durationSeconds: first.duration ?? null,
        totalCostUsd: first.total_cost_usd ?? null,
        actualCommands: first.actual_commands ?? null,
        tags: first.tags ?? [],
        startTime: data.start_time ?? null,
    };
}

export async function listAllRuns(): Promise<RunSummary[]> {
    const ids = await listRunIds();
    const results = await Promise.all(ids.map((id) => readRunSummary(id)));
    return results.filter((r): r is RunSummary => r !== null);
}

async function findTaskSubdir(runId: string): Promise<string | null> {
    const defaultDir = path.join(RUNS_DIR, runId, "default");
    const entries = await fs
        .readdir(defaultDir, { withFileTypes: true })
        .catch(() => []);
    const taskDir = entries.find(
        (e) => e.isDirectory() && e.name.startsWith("skill-"),
    );
    return taskDir ? taskDir.name : null;
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

export function parseFlowDebug(
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

export function parseToolCalls(turns: TurnEntry[], max = 200): ToolCall[] {
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

// ---------- Readers ----------

export async function readRunDetail(id: string): Promise<RunDetail | null> {
    const summary = await readRunSummary(id);
    if (!summary) return null;

    const taskSubdir = await findTaskSubdir(id);
    if (!taskSubdir) {
        return {
            ...summary,
            finalStatus: null,
            errorMessage: null,
            taskDescription: null,
            criteria: [],
            artifacts: [],
            logPath: null,
            taskJsonPath: null,
            taskSubdir: null,
            flowDebug: null,
            toolCalls: [],
        };
    }

    const taskDir = path.join(RUNS_DIR, id, "default", taskSubdir);
    const taskJsonPath = path.join(taskDir, "task.json");
    const logPath = path.join(taskDir, "task.log");

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
    }>(taskJsonPath);

    const criteria: CriterionResult[] = (
        task?.success_criteria_results ?? []
    ).map((c) => ({
        criterionType: c.criterion_type ?? null,
        description: c.description ?? null,
        score: c.score ?? null,
        details: c.details ?? null,
        error: c.error ?? null,
    }));

    const artifactRoot = path.join(taskDir, "artifacts");
    // relPath is stored relative to the run root so the /api/file route can
    // resolve it against RUNS_DIR/<runId> without needing to know the task
    // subdir.
    const artifactPrefix = path.relative(
        path.join(RUNS_DIR, id),
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
        ...summary,
        finalStatus: task?.final_status ?? null,
        errorMessage: task?.error_message ?? null,
        taskDescription,
        criteria,
        artifacts,
        logPath,
        taskJsonPath,
        taskSubdir,
        flowDebug,
        toolCalls,
    };
}

export async function readLogTail(
    runId: string,
    taskSubdir: string,
    maxBytes = 200_000,
): Promise<string> {
    const logPath = path.join(RUNS_DIR, runId, "default", taskSubdir, "task.log");
    const raw = await fs.readFile(logPath, "utf-8").catch(() => "");
    if (raw.length <= maxBytes) return raw;
    return `… (truncated, showing last ${maxBytes} bytes)\n\n${raw.slice(-maxBytes)}`;
}

export async function resolveSafePath(
    runId: string,
    relPath: string,
): Promise<string | null> {
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
