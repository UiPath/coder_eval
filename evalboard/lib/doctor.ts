// The skill-doctor report of one nightly run (contract v1), read from
// `<runId>/skill-doctor/`:
//   - status.json: pointer + lock, changes while the doctor runs;
//   - triage-<build>.json: the per-task failure analysis, immutable;
//   - <fixes_dir>/fixes.json: fix candidates and pr_checks, optional, create-only;
//   - <fixes_dir>/opened.json: the PRs the doctor's open step opened, optional.
//
// Pure data + string helpers, no Node builtins: the parsed shapes are passed to
// the run page's components, and the tests import this module directly. The
// types are written by hand against the doctor's status_v1 / triage_v1 /
// fixes_v1 schemas and carry only the fields the run page renders. A file that does not parse,
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

export const DOCTOR_STATES = [
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
    triageFile: string | null;
    fixesDir: string | null;
    trigger: string | null;
}

// Every PR URL the parsers hand out has passed isSkillsPrUrl: views can link
// them without checking again.
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
    // Open/merged PRs on the task's files; only used to resolve fixedByPr.
    existingPrs: DoctorPr[];
    // The diagnosis's judgement of the open PR that best fixes this row.
    fixedByPr: DoctorFixedByPr | null;
    envGroup: string | null;
    // The doctor's PR for this task, allowlisted (else null).
    doctorPr: string | null;
}

export interface DoctorFixedByPr {
    number: number;
    verdict: "fixes" | "may_help" | "unrelated";
    basis: string;
}

// The Fix stage's replay of a row's fixedByPr (fixes.json pr_checks). Its
// url is validated (allowlisted, ends in /pull/<number>) but not kept: the
// board links the PR from the row's existing PRs.
export interface DoctorPrCheck {
    number: number;
    verdict: "verified" | "not_verified" | "untested";
    evidence: string;
    reason: "needs-windows-run" | null;
    // The PR head the check applied (40 hex), when the doctor recorded it.
    headSha: string | null;
}

export interface DoctorAction {
    count: number;
    text: string;
    taskIds: string[];
}

export interface DoctorTriage {
    // The run the triage describes (run.run_id); readRunDoctor drops a triage
    // that names another run.
    runId: string | null;
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
    patchLines: { added: number; removed: number } | null;
    title: string;
    gates: DoctorGates;
}

export interface DoctorFixes {
    // The run fixes.json describes (run_id); readRunDoctor drops one that
    // names another run.
    runId: string | null;
    fixMode: string | null;
    // By task id (own keys; read with own()). null: an entry that did not
    // validate.
    prChecks: Record<string, DoctorPrCheck | null>;
    candidates: DoctorFix[];
}

export interface RunDoctor {
    status: DoctorStatus;
    // Set only when the status is `done` and its triage file parsed.
    triage: DoctorTriage | null;
    fixes: DoctorFixes | null;
    // PRs the doctor opened (`<fixes_dir>/opened.json`): task id → PR URL,
    // allowlisted URLs only.
    opened?: Record<string, string>;
    // A doctor file could not be fetched (a storage error, not a 404): the
    // parts that are missing may exist.
    fetchFailed?: boolean;
}

// ---------- coercion helpers ----------

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
    return typeof v === "object" && v !== null && !Array.isArray(v);
}
function str(v: unknown): string | null {
    return typeof v === "string" ? v : null;
}
function isCount(v: unknown): v is number {
    return typeof v === "number" && Number.isInteger(v) && v >= 0;
}
function int(v: unknown, dflt = 0): number {
    return isCount(v) ? v : dflt;
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
        // A name outside the pattern is treated as absent: it lands in a path.
        triageFile: triageFile && TRIAGE_FILE_RE.test(triageFile) ? triageFile : null,
        fixesDir: fixesDir && FIXES_DIR_RE.test(fixesDir) ? fixesDir : null,
        trigger: str(j.trigger),
    };
}

function parsePr(v: unknown): DoctorPr | null {
    if (!isObj(v)) return null;
    const url = str(v.url);
    if (!isSkillsPrUrl(url)) return null;
    return { url, title: str(v.title) ?? "", state: str(v.state) ?? "unknown" };
}

function parseFixedByPr(v: unknown): DoctorFixedByPr | null {
    if (!isObj(v)) return null;
    const n = v.number;
    const verdict = v.verdict;
    if (typeof n !== "number" || !Number.isInteger(n) || n < 1) return null;
    if (verdict !== "fixes" && verdict !== "may_help" && verdict !== "unrelated") return null;
    return { number: n, verdict, basis: (str(v.basis) ?? "").slice(0, 300) };
}

function parsePrCheck(v: unknown): DoctorPrCheck | null {
    if (!isObj(v)) return null;
    const n = v.number;
    const verdict = v.verdict;
    if (typeof n !== "number" || !Number.isInteger(n) || n < 1) return null;
    if (!isSkillsPrUrl(v.url) || !v.url.endsWith(`/pull/${n}`)) return null;
    if (verdict !== "verified" && verdict !== "not_verified" && verdict !== "untested") return null;
    return {
        number: n,
        verdict,
        evidence: (str(v.evidence) ?? "").slice(0, 600),
        reason: v.reason === "needs-windows-run" ? "needs-windows-run" : null,
        headSha: typeof v.head_sha === "string" && /^[0-9a-f]{40}$/.test(v.head_sha) ? v.head_sha : null,
    };
}

// A malformed entry is kept as null, so its task shows no PR row rather than
// falling back to "no check" (which would read as "may fix").
function parsePrChecks(v: unknown): Record<string, DoctorPrCheck | null> {
    const out = new Map<string, DoctorPrCheck | null>();
    if (isObj(v)) {
        for (const [k, c] of Object.entries(v)) out.set(k, parsePrCheck(c));
    }
    return Object.fromEntries(out);
}

/** A report-keyed lookup that never reads the prototype ("constructor", ...). */
export function own<T>(r: Record<string, T> | null | undefined, key: string): T | undefined {
    return r && Object.hasOwn(r, key) ? r[key] : undefined;
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
        existingPrs: prList(v.existing_prs),
        fixedByPr: parseFixedByPr(v.fixed_by_pr),
        envGroup: str(v.env_group),
        doctorPr: isSkillsPrUrl(v.doctor_pr) ? v.doctor_pr : null,
    };
}

export function parseTriage(raw: string | null | undefined): DoctorTriage | null {
    const j = parseJson(raw);
    if (!isObj(j) || j.schema_version !== 1) return null;
    if (!isObj(j.summary) || !Array.isArray(j.tasks)) return null;
    const s = j.summary;
    // The header shows these counts: a missing or malformed one is contract
    // drift, so the triage does not parse (and readRunDoctor logs it) rather
    // than reading as a real 0.
    if (!isCount(s.rows_failed) || !isCount(s.rows_diagnosed)) return null;
    const run = isObj(j.run) ? j.run : {};
    return {
        runId: str(run.run_id),
        rowsRun: isCount(run.rows_executed) ? run.rows_executed : null,
        rowsFailed: s.rows_failed,
        rowsDiagnosed: s.rows_diagnosed,
        tasks: j.tasks.map(parseTask).filter((t): t is DoctorTask => t !== null),
        actions: Array.isArray(j.actions)
            ? j.actions.flatMap((a): DoctorAction[] =>
                  // "open-pr" actions list PRs that merely touch the files, not
                  // fixes: the board leaves them out.
                  isObj(a) && typeof a.text === "string" && a.kind !== "open-pr"
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
        m && isCount(m.n) && isCount(m.killed) ? { n: m.n, killed: m.killed } : null;
    return { replay, mutants, review: str(v.review) };
}

export function parseFixes(raw: string | null | undefined): DoctorFixes | null {
    const j = parseJson(raw);
    if (!isObj(j) || j.schema_version !== 1 || !Array.isArray(j.candidates)) return null;
    return {
        runId: str(j.run_id),
        fixMode: str(j.fix_mode),
        prChecks: parsePrChecks(j.pr_checks),
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

/**
 * `<fixes_dir>/opened.json` from the doctor's open step:
 * `{"opened": [{"task_id", "pr_url", ...} | {"task_id", "problem"}], "error"?}`.
 * Untrusted: only entries with a task id and an allowlisted PR URL map;
 * problem entries and anything else are skipped. Null when it does not parse.
 */
export function parseOpened(raw: string | null | undefined): Record<string, string> | null {
    const j = parseJson(raw);
    if (!isObj(j) || !Array.isArray(j.opened)) return null;
    // Built through a Map so a task id such as "__proto__" or "constructor"
    // is an own key, never the prototype; the first entry per task wins.
    const out = new Map<string, string>();
    for (const e of j.opened) {
        if (!isObj(e)) continue;
        const id = str(e.task_id);
        const url = str(e.pr_url);
        if (id && isSkillsPrUrl(url) && !out.has(id)) out.set(id, url);
    }
    return Object.fromEntries(out);
}

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
