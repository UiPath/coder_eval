// The skill-doctor report of one nightly run (contract v1), read from
// `<runId>/skill-doctor/`:
//   - status.json: pointer + lock, changes while the doctor runs;
//   - triage-<build>.json: the per-task failure analysis, immutable;
//   - <fixes_dir>/fixes.json: proposed fixes, optional, create-only.
//
// Pure data + string helpers, no Node builtins: the parsed shapes are passed to
// the run page's components, and the tests import this module directly. The
// types are written by hand against the doctor's status_v1 / triage_v1 schemas
// and carry only the fields the run page renders. A file that does not parse,
// or whose schema_version is not 1, is treated as absent: the page must never
// fail because of a doctor file.

import { isValidTaskId } from "./ids";
import { SKILLS_SOURCE } from "./sources";

export const DOCTOR_DIR = "skill-doctor";
const TRIAGE_FILE_RE = /^triage-[A-Za-z0-9._-]+\.json$/;
const FIXES_DIR_RE = /^fixes(-[0-9]+)?$/;
// Candidate ids name `<id>.patch` inside the fixes dir.
const PATCH_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
// At most this many patches per run are fetched, linked and served.
const MAX_PATCHES = 10;

const DOCTOR_STATES = [
    "queued",
    "running",
    "done",
    "failed",
    "skipped",
    "retracted",
] as const;
export type DoctorState = (typeof DOCTOR_STATES)[number];

function isDoctorState(v: unknown): v is DoctorState {
    return typeof v === "string" && (DOCTOR_STATES as readonly string[]).includes(v);
}

export interface DoctorStatus {
    state: DoctorState;
    reason: string;
    startedAt: string | null;
    buildUrl: string | null;
    costUsd: number | null;
    triageFile: string | null;
    fixesDir: string | null;
    trigger: string | null;
}

export interface DoctorPr {
    url: string;
    title: string;
    state: string;
}

// One failed row, with the fields the skill-doctor page shows on its card.
export interface DoctorTask {
    taskId: string;
    skill: string;
    layer: string;
    solvable: string | null;
    runStatus: string | null;
    confidence: string | null;
    why: string | null;
    fix: string | null;
    criterion: string | null;
    evidence: string | null;
    passedBefore: string | null;
    related: string | null;
    existingPrs: DoctorPr[];
    relatedSkillPrs: DoctorPr[];
    envGroup: string | null;
    doctorPr: string | null;
}

export interface DoctorAction {
    count: number;
    text: string;
    taskIds: string[];
}

export interface DoctorTriage {
    // Rows the nightly executed (null when the run did not say).
    rowsRun: number | null;
    rowsFailed: number;
    rowsDiagnosed: number;
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
    // Prepared PR bodies (`<fixes_dir>/<id>.md`) of pr-ready candidates in
    // patchIds, by candidate id. Markdown text, rendered without raw HTML.
    fixBodies?: Record<string, string>;
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
    if (!isObj(j) || j.schema_version !== 1 || !isDoctorState(j.state)) return null;
    const triageFile = str(j.triage_file);
    const fixesDir = str(j.fixes_dir);
    return {
        state: j.state,
        reason: str(j.reason) ?? "",
        startedAt: str(j.started_at),
        buildUrl: str(j.build_url),
        costUsd: num(j.cost_usd),
        // A name outside the pattern is treated as absent: it lands in a path.
        triageFile: triageFile && TRIAGE_FILE_RE.test(triageFile) ? triageFile : null,
        fixesDir: fixesDir && FIXES_DIR_RE.test(fixesDir) ? fixesDir : null,
        trigger: str(j.trigger),
    };
}

function parsePr(v: unknown): DoctorPr | null {
    if (!isObj(v)) return null;
    const url = str(v.url);
    if (!url) return null;
    return { url, title: str(v.title) ?? "", state: str(v.state) ?? "unknown" };
}

function prList(v: unknown): DoctorPr[] {
    return Array.isArray(v) ? v.map(parsePr).filter((p): p is DoctorPr => p !== null) : [];
}

function parseTask(v: unknown): DoctorTask | null {
    if (!isObj(v)) return null;
    const taskId = str(v.task_id);
    if (!taskId) return null;
    return {
        taskId,
        skill: str(v.skill) ?? "",
        layer: str(v.layer) ?? "undiagnosed",
        solvable: str(v.solvable),
        runStatus: str(v.run_status),
        confidence: str(v.confidence),
        why: str(v.why),
        fix: str(v.fix),
        criterion: str(v.criterion),
        evidence: str(v.evidence),
        passedBefore: str(v.passed_before),
        related: str(v.related),
        existingPrs: prList(v.existing_prs),
        relatedSkillPrs: prList(v.related_skill_prs),
        envGroup: str(v.env_group),
        doctorPr: str(v.doctor_pr),
    };
}

export function parseTriage(raw: string | null | undefined): DoctorTriage | null {
    const j = parseJson(raw);
    if (!isObj(j) || j.schema_version !== 1) return null;
    if (!isObj(j.summary) || !Array.isArray(j.tasks)) return null;
    const s = j.summary;
    const run = isObj(j.run) ? j.run : {};
    return {
        rowsRun: typeof run.rows_executed === "number" ? int(run.rows_executed) : null,
        rowsFailed: int(s.rows_failed),
        rowsDiagnosed: int(s.rows_diagnosed),
        tasks: j.tasks.map(parseTask).filter((t): t is DoctorTask => t !== null),
        actions: Array.isArray(j.actions)
            ? j.actions.flatMap((a): DoctorAction[] =>
                  isObj(a) && typeof a.text === "string"
                      ? [{ count: int(a.count), text: a.text, taskIds: strList(a.task_ids) }]
                      : [],
              )
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
    if (!isObj(j) || j.schema_version !== 1 || !Array.isArray(j.candidates)) return null;
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
                    gates: parseGates(c.gates),
                },
            ];
        }),
    };
}

/**
 * The only patches the board touches. readRunDoctor fetches exactly these,
 * the Fixes table links exactly these, and /api/file serves only these (via
 * readRunDoctor, so there is one fetch path). A patch exists only when
 * fixes.json reports patch lines; the id must be a safe file name; at most
 * MAX_PATCHES per run.
 */
export function patchIds(fixes: DoctorFixes | null): Set<string> {
    const ids = new Set<string>();
    for (const c of fixes?.candidates ?? []) {
        if (ids.size >= MAX_PATCHES) break;
        if (c.patchLines !== null && PATCH_ID_RE.test(c.id)) ids.add(c.id);
    }
    return ids;
}

/** The patchIds whose prepared PR body (`<id>.md`) the board loads: pr-ready only. */
export function bodyIds(fixes: DoctorFixes | null): string[] {
    const ids = patchIds(fixes);
    return (fixes?.candidates ?? [])
        .filter((c) => c.outcome === "pr-ready" && ids.has(c.id))
        .map((c) => c.id);
}

// A prepared PR body is a few KB; anything far larger is cut.
export const MAX_BODY_CHARS = 64_000;

/** `<fixes_dir>/<id>.patch` → the id, or null for any other doctor path. */
export function patchIdFromPath(rel: string): { fixesDir: string; id: string } | null {
    const parts = rel.split("/");
    if (parts.length !== 2 || !FIXES_DIR_RE.test(parts[0])) return null;
    const m = /^(.+)\.patch$/.exec(parts[1]);
    if (!m || !PATCH_ID_RE.test(m[1])) return null;
    return { fixesDir: parts[0], id: m[1] };
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

/** A task link is built only from an id the task page itself accepts. */
export const isLinkableTaskId = isValidTaskId;

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

/**
 * The panel is internal-only, for the skills nightly, with an env kill switch
 * (EVALBOARD_TASK_ANALYSIS=off, in any case).
 */
export function taskAnalysisEnabled(
    internal: boolean,
    sourceId: string,
    env: string | undefined,
): boolean {
    return (
        internal &&
        sourceId === SKILLS_SOURCE.id &&
        (env ?? "").trim().toLowerCase() !== "off"
    );
}

export interface AnalysisPlan {
    // Markdown for the AI summary panel (null: no panel).
    summary: string | null;
    // The Task Failure Analysis block, right under the summary (null: none).
    doctor: RunDoctor | null;
}

/**
 * What the run page shows. With a done report whose triage parsed, the
 * summary stops at analysis.md's `## Findings` line (the block replaces the
 * rest), or keeps the whole file when there is no such line. In every other
 * state the summary is unchanged (Findings included) and the block shows only
 * the doctor's state.
 */
export function planAnalysis(
    analysis: string | null,
    doctor: RunDoctor | null,
): AnalysisPlan {
    if (doctor && doctor.status.state === "done" && doctor.triage) {
        const split = analysis ? splitAnalysis(analysis) : null;
        const summary = split ? split.top : analysis;
        return { summary: summary && summary.trim() ? summary : null, doctor };
    }
    return { summary: analysis, doctor };
}
