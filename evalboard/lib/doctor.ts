// The skill-doctor report of one nightly run (contract v1), read from
// `<runId>/skill-doctor/`:
//   - status.json: pointer + lock, changes while the doctor runs;
//   - triage-<build>.json: the per-task failure analysis, immutable;
//   - <fixes_dir>/fixes.json: proposed fixes, optional, create-only.
//
// Pure data + string helpers, no Node builtins: the parsed shapes are passed to
// the run page's components, and the tests import this module directly. The
// types are written by hand against the doctor's status_v1 / triage_v1 schemas
// and keep only the fields the board shows. Anything that does not parse, or a
// schema_version other than 1, is treated as absent: the page must never fail
// because of a doctor file.

export const DOCTOR_DIR = "skill-doctor";
export const TRIAGE_FILE_RE = /^triage-[A-Za-z0-9._-]+\.json$/;
export const FIXES_DIR_RE = /^fixes(-[0-9]+)?$/;
// Candidate ids name `<id>.patch` inside the fixes dir.
export const PATCH_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export const DOCTOR_STATES = [
    "queued",
    "running",
    "done",
    "failed",
    "skipped",
    "retracted",
] as const;
export type DoctorState = (typeof DOCTOR_STATES)[number];

export interface DoctorFixesDigest {
    fixMode: string | null;
    costUsd: number | null;
    totalCapUsd: number | null;
    counts: Record<string, number>;
}

export interface DoctorStatus {
    state: DoctorState;
    reason: string;
    startedAt: string | null;
    finishedAt: string | null;
    buildUrl: string | null;
    costUsd: number | null;
    rowsFailed: number | null;
    rowsDiagnosed: number | null;
    triageFile: string | null;
    fixesDir: string | null;
    fixes: DoctorFixesDigest | null;
    trigger: string | null;
}

export interface DoctorPr {
    url: string;
    title: string;
    state: string;
}

export interface DoctorTask {
    taskId: string;
    skill: string;
    layer: string;
    cls: string;
    confidence: string | null;
    contested: string[] | null;
    criterion: string | null;
    diagnosed: boolean;
    why: string | null;
    fix: string | null;
    solvable: string | null;
    state: string | null;
    nightsSeen: number;
    owners: string[];
    existingPrs: DoctorPr[];
    doctorPr: string | null;
    evidence: string | null;
    envGroup: string | null;
}

export interface DoctorAction {
    kind: string;
    count: number;
    text: string;
    taskIds: string[];
}

export interface DoctorTriage {
    disclaimer: string;
    costUsd: number | null;
    buildUrl: string | null;
    summary: {
        rowsFailed: number;
        rowsDiagnosed: number;
        byClass: Record<string, number>;
        byLayer: Record<string, number>;
        vsHistory: {
            new: number;
            persisting: number;
            fixed: number;
            baseRun: string | null;
        } | null;
    };
    tasks: DoctorTask[];
    actions: DoctorAction[];
}

export interface DoctorGates {
    replay: string | null;
    mutants: { n: number; killed: number } | null;
    review: string | null;
}

export interface DoctorFix {
    id: string;
    taskId: string;
    covers: string[];
    outcome: string;
    costUsd: number | null;
    patchLines: { added: number; removed: number } | null;
    title: string;
    problem: string;
    gates: DoctorGates;
}

export interface DoctorFixes {
    fixMode: string | null;
    costUsd: number | null;
    totalCapUsd: number | null;
    candidates: DoctorFix[];
}

export interface RunDoctor {
    status: DoctorStatus;
    // Set only when the status is `done` and its triage file parsed.
    triage: DoctorTriage | null;
    fixes: DoctorFixes | null;
}

// ---------- coercion helpers ----------

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
    return typeof v === "object" && v !== null && !Array.isArray(v);
}
function str(v: unknown): string | null {
    return typeof v === "string" ? v : null;
}
function num(v: unknown): number | null {
    return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function int(v: unknown, dflt = 0): number {
    return typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : dflt;
}
function strList(v: unknown): string[] {
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}
function countMap(v: unknown): Record<string, number> {
    const out: Record<string, number> = {};
    if (!isObj(v)) return out;
    for (const [k, n] of Object.entries(v)) {
        if (typeof n === "number" && Number.isInteger(n) && n >= 0) out[k] = n;
    }
    return out;
}

function parseJson(raw: string | null | undefined): unknown {
    if (raw == null) return null;
    try {
        return JSON.parse(raw);
    } catch {
        return null;
    }
}

// ---------- parsers ----------

export function parseDoctorStatus(raw: string | null | undefined): DoctorStatus | null {
    const j = parseJson(raw);
    if (!isObj(j) || j.schema_version !== 1) return null;
    const state = j.state;
    if (typeof state !== "string" || !(DOCTOR_STATES as readonly string[]).includes(state))
        return null;
    const triageFile = str(j.triage_file);
    const fixesDir = str(j.fixes_dir);
    let fixes: DoctorFixesDigest | null = null;
    if (isObj(j.fixes)) {
        fixes = {
            fixMode: str(j.fixes.fix_mode),
            costUsd: num(j.fixes.cost_usd),
            totalCapUsd: num(j.fixes.total_cap_usd),
            counts: countMap(j.fixes.counts),
        };
    }
    return {
        state: state as DoctorState,
        reason: str(j.reason) ?? "",
        startedAt: str(j.started_at),
        finishedAt: str(j.finished_at),
        buildUrl: str(j.build_url),
        costUsd: num(j.cost_usd),
        rowsFailed: typeof j.rows_failed === "number" ? int(j.rows_failed) : null,
        rowsDiagnosed: typeof j.rows_diagnosed === "number" ? int(j.rows_diagnosed) : null,
        // A name outside the pattern is treated as absent: it lands in a path.
        triageFile: triageFile && TRIAGE_FILE_RE.test(triageFile) ? triageFile : null,
        fixesDir: fixesDir && FIXES_DIR_RE.test(fixesDir) ? fixesDir : null,
        fixes,
        trigger: str(j.trigger),
    };
}

function parsePr(v: unknown): DoctorPr | null {
    if (!isObj(v)) return null;
    const url = str(v.url);
    if (!url) return null;
    return { url, title: str(v.title) ?? "", state: str(v.state) ?? "unknown" };
}

function parseTask(v: unknown): DoctorTask | null {
    if (!isObj(v)) return null;
    const taskId = str(v.task_id);
    if (!taskId) return null;
    return {
        taskId,
        skill: str(v.skill) ?? "",
        layer: str(v.layer) ?? "undiagnosed",
        cls: str(v.class) ?? "",
        confidence: str(v.confidence),
        contested: Array.isArray(v.contested) ? strList(v.contested) : null,
        criterion: str(v.criterion),
        diagnosed: v.diagnosed === true,
        why: str(v.why),
        fix: str(v.fix),
        solvable: str(v.solvable),
        state: str(v.state),
        nightsSeen: int(v.nights_seen, 1),
        owners: strList(v.owners),
        existingPrs: Array.isArray(v.existing_prs)
            ? v.existing_prs.map(parsePr).filter((p): p is DoctorPr => p !== null)
            : [],
        doctorPr: str(v.doctor_pr),
        evidence: str(v.evidence),
        envGroup: str(v.env_group),
    };
}

export function parseTriage(raw: string | null | undefined): DoctorTriage | null {
    const j = parseJson(raw);
    if (!isObj(j) || j.schema_version !== 1) return null;
    if (!isObj(j.summary) || !Array.isArray(j.tasks)) return null;
    const s = j.summary;
    const run = isObj(j.run) ? j.run : {};
    const vh = isObj(s.vs_history) ? s.vs_history : null;
    return {
        disclaimer: str(j.disclaimer) ?? "",
        costUsd: num(run.cost_usd),
        buildUrl: str(run.build_url),
        summary: {
            rowsFailed: int(s.rows_failed),
            rowsDiagnosed: int(s.rows_diagnosed),
            byClass: countMap(s.by_class),
            byLayer: countMap(s.by_layer),
            vsHistory: vh
                ? {
                      new: int(vh.new),
                      persisting: int(vh.persisting),
                      fixed: int(vh.fixed),
                      baseRun: str(vh.base_run),
                  }
                : null,
        },
        tasks: j.tasks.map(parseTask).filter((t): t is DoctorTask => t !== null),
        actions: Array.isArray(j.actions)
            ? j.actions.flatMap((a): DoctorAction[] => {
                  if (!isObj(a) || typeof a.text !== "string") return [];
                  return [
                      {
                          kind: str(a.kind) ?? "",
                          count: int(a.count),
                          text: a.text,
                          taskIds: strList(a.task_ids),
                      },
                  ];
              })
            : [],
    };
}

function parseGates(v: unknown): DoctorGates {
    if (!isObj(v)) return { replay: null, mutants: null, review: null };
    const replay = isObj(v.replay) ? str(v.replay.status) : null;
    const m = isObj(v.mutants) ? v.mutants : null;
    const mutants =
        m && typeof m.n === "number" && typeof m.killed === "number"
            ? { n: int(m.n), killed: int(m.killed) }
            : null;
    return { replay, mutants, review: str(v.review) };
}

export function parseFixes(raw: string | null | undefined): DoctorFixes | null {
    const j = parseJson(raw);
    if (!isObj(j) || !Array.isArray(j.candidates)) return null;
    // fixes.json carries its own schema_version 1; an unknown one is absent.
    if (j.schema_version !== undefined && j.schema_version !== 1) return null;
    return {
        fixMode: str(j.fix_mode),
        costUsd: num(j.cost_usd),
        totalCapUsd: num(j.total_cap_usd),
        candidates: j.candidates.flatMap((c): DoctorFix[] => {
            if (!isObj(c)) return [];
            const id = str(c.id);
            const taskId = str(c.task_id);
            if (!id || !taskId) return [];
            const pl = isObj(c.patch_lines) ? c.patch_lines : null;
            return [
                {
                    id,
                    taskId,
                    covers: strList(c.covers),
                    outcome: str(c.outcome) ?? "unknown",
                    costUsd: num(c.cost_usd),
                    patchLines: pl
                        ? { added: int(pl.added), removed: int(pl.removed) }
                        : null,
                    title: str(c.title) ?? "",
                    problem: str(c.problem) ?? "",
                    gates: parseGates(c.gates),
                },
            ];
        }),
    };
}

/** Patch ids whose `<id>.patch` the board fetches and links. */
export function patchIds(fixes: DoctorFixes | null): string[] {
    if (!fixes) return [];
    return fixes.candidates
        .filter((c) => c.patchLines !== null && PATCH_ID_RE.test(c.id))
        .map((c) => c.id);
}

// ---------- links ----------

// The report's text comes from a model and from the run's own logs, so a URL
// in it is shown as a link only when it has exactly the expected shape.
const SKILLS_PR_RE = /^https:\/\/github\.com\/UiPath\/skills\/pull\/\d+$/;
const ADO_RE = /^https:\/\/dev\.azure\.com\/[^\s"'<>]*$/;

export function isSkillsPrUrl(u: unknown): u is string {
    return typeof u === "string" && SKILLS_PR_RE.test(u);
}

export function isAdoUrl(u: unknown): u is string {
    return typeof u === "string" && ADO_RE.test(u);
}

// Same shape as blob.ts's task-id check (kept here so this module stays free
// of Node builtins): a task link is only built from an id the task page accepts.
const TASK_ID_RE = /^[\w.-]+(\/[\w.-]+)*$/;

export function isLinkableTaskId(id: string): boolean {
    return (
        id.length > 0 &&
        id.length < 256 &&
        TASK_ID_RE.test(id) &&
        !id.split("/").some((s) => s === "." || s === "..")
    );
}

// ---------- analysis.md split ----------

/**
 * Cut analysis.md at its first line that is exactly `## Findings`, outside a
 * fenced code block. Null when there is no such line.
 */
export function splitAnalysis(
    md: string,
): { top: string; findings: string } | null {
    const lines = md.split("\n");
    let fence: string | null = null;
    let offset = 0;
    for (const raw of lines) {
        const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
        const f = /^ {0,3}(`{3,}|~{3,})/.exec(line);
        if (f) {
            const mark = f[1];
            if (fence === null) fence = mark;
            else if (mark[0] === fence[0] && mark.length >= fence.length && line.trim() === mark)
                fence = null;
        } else if (fence === null && line === "## Findings") {
            return { top: md.slice(0, offset), findings: md.slice(offset) };
        }
        offset += raw.length + 1;
    }
    return null;
}

// ---------- gating + page plan ----------

/** The panel is internal-only, for the skills nightly, with an env kill switch. */
export function taskAnalysisEnabled(
    internal: boolean,
    sourceId: string,
    env: string | undefined,
): boolean {
    return internal && sourceId === "skills" && env !== "off";
}

export interface AnalysisPlan {
    // Markdown for the AI summary panel (null: no panel).
    summary: string | null;
    // Render the Task Failure Analysis panel in place of the cut-off part.
    panel: RunDoctor | null;
    // One-line doctor status under the summary (null: none).
    statusLine: DoctorStatus | null;
}

/**
 * What the run page shows. The panel replaces Findings and below only when
 * the report is done and analysis.md has a split point; every other case keeps
 * today's view, plus a status line when a status exists.
 */
export function planAnalysis(
    analysis: string | null,
    doctor: RunDoctor | null,
): AnalysisPlan {
    if (!doctor) return { summary: analysis, panel: null, statusLine: null };
    const { status } = doctor;
    if (status.state === "done" && doctor.triage && analysis) {
        const split = splitAnalysis(analysis);
        if (split) {
            return {
                summary: split.top.trim() ? split.top : null,
                panel: doctor,
                statusLine: null,
            };
        }
    }
    return { summary: analysis, panel: null, statusLine: status };
}
