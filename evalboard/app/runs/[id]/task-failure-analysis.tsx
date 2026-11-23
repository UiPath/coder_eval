"use client";

import Link from "next/link";
import { useMemo, useState, type ReactNode } from "react";
import { withSource } from "@/app/_lib/source-param";
import { fmtTimestamp } from "@/lib/format";
import { SummaryMarkdown } from "./analysis-panel";
import {
    DOCTOR_DIR,
    isAdoUrl,
    isLinkableTaskId,
    isSkillsPrUrl,
    own,
    patchIds,
    sectionHeadings,
    type DoctorFix,
    type DoctorPr,
    type DoctorStatus,
    type DoctorTask,
    type DoctorTriage,
    type RunDoctor,
} from "@/lib/doctor";

// The skill-doctor report for one run, drawn natively from its JSON in the
// order of the doctor's own page (stats → failures by layer → Fix first →
// skill failures), never by embedding that page. Every string is plain text;
// the only links are this board's task pages and URLs of an exact known shape.

// Layer order, labels and colors, as on the doctor's page. Maps, not object
// literals: layer and solvable come from the report, and a key such as
// "constructor" or "__proto__" must not reach the object prototype.
const LAYERS = new Map<string, { label: string; color: string }>([
    ["env", { label: "Environment", color: "#56708f" }],
    ["grader", { label: "Grader", color: "#b07410" }],
    ["task", { label: "Task setup", color: "#7a4fb0" }],
    ["agent", { label: "Agent mistake", color: "#c2413a" }],
    ["skill", { label: "Skill", color: "#d0661c" }],
    ["upstream", { label: "Upstream (CLI)", color: "#2f6fd0" }],
    ["flaky", { label: "Flaky", color: "#7d8794" }],
    ["undiagnosed", { label: "Not diagnosed", color: "#9aa5b3" }],
]);
export const LAYER_ORDER = [...LAYERS.keys()];
const OTHER_COLOR = "#9aa5b3";
const layerOf = (k: string) => LAYERS.get(k) ?? { label: k, color: OTHER_COLOR };
const rank = (k: string) => {
    const i = LAYER_ORDER.indexOf(k);
    return i === -1 ? LAYER_ORDER.length : i;
};

const SOLVABLE = new Map<string, string>([
    ["env-fix", "Fix the tenant"],
    ["yes-grader", "Fixable: grader"],
    ["yes-task", "Fixable: task"],
    ["yes-skill", "Fixable: skill"],
    ["upstream-report", "Upstream report"],
    ["no", "Not a repo fix"],
]);
const isFixable = (t: DoctorTask) => /^yes/.test(t.solvable ?? "");
const isReady = (c: DoctorFix) => c.outcome === "pr-ready";

/** Tasks per layer, in the doctor page's layer order. */
export function layerCounts(tasks: DoctorTask[]): [string, number][] {
    const m = new Map<string, number>();
    for (const t of tasks) m.set(t.layer, (m.get(t.layer) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => rank(a[0]) - rank(b[0]));
}

// ---------- text ----------

/**
 * Plain text with `code` spans, and URLs turned into links only when they
 * pass an allowlist (a UiPath/skills PR or a dev.azure.com build).
 */
export function RichText({ text }: { text: string }) {
    const out: ReactNode[] = [];
    text.split(/(`[^`]+`)/).forEach((part, i) => {
        if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) {
            out.push(
                <code key={i} className="rounded bg-gray-100 px-1 text-[0.92em]">
                    {part.slice(1, -1)}
                </code>,
            );
            return;
        }
        part.split(/(https:\/\/[^\s)]+)/).forEach((seg, j) => {
            if (!seg) return;
            const url = seg.replace(/[.,;:]+$/, "");
            if (isSkillsPrUrl(url) || isAdoUrl(url)) {
                out.push(
                    <ExtA key={`${i}-${j}`} href={url}>
                        {url}
                    </ExtA>,
                    seg.slice(url.length),
                );
            } else out.push(seg);
        });
    });
    return <>{out}</>;
}

/** An external link, opened in a new tab. Callers pass only allowlisted URLs. */
function ExtA({
    href,
    className = "text-studio-blue hover:underline",
    children,
}: {
    href: string;
    className?: string;
    children: ReactNode;
}) {
    return (
        <a href={href} target="_blank" rel="noopener noreferrer" className={className}>
            {children}
        </a>
    );
}

/** A build link only for a dev.azure.com URL; anything else is the label as text. */
function BuildLink({ href, label }: { href: string; label: string }) {
    if (!isAdoUrl(href)) return <span>{label}</span>;
    return <ExtA href={href}>{label}</ExtA>;
}

function prLabel(url: string): string {
    const m = /\/pull\/(\d+)$/.exec(url);
    return m ? `#${m[1]}` : url;
}

// ---------- digest + state ----------

export function digest(doctor: RunDoctor): string {
    const { status, triage } = doctor;
    switch (status.state) {
        case "queued":
        case "running":
            return status.startedAt
                ? `${status.state} since ${fmtTimestamp(status.startedAt)}`
                : status.state;
        case "failed":
        case "skipped":
            return status.reason ? `${status.state}: ${status.reason}` : status.state;
        case "retracted":
            return "the report for this run was retracted";
        case "done": {
            if (!triage)
                return doctor.fetchFailed
                    ? "report done, but its triage file could not be fetched"
                    : "report done, but its triage file is missing or did not parse";
            const n = countFixPrs(doctor, triage.tasks);
            const failed = `${triage.rowsFailed} failed`;
            return n ? `${failed} · ${prsWithFix(n)}` : failed;
        }
    }
}

// ---------- fixes ----------

function patchHref(runId: string, sourceId: string, dir: string, id: string) {
    return withSource(
        `/api/file?run=${encodeURIComponent(runId)}&path=${encodeURIComponent(`${DOCTOR_DIR}/${dir}/${id}.patch`)}`,
        sourceId,
    );
}

function gateText(f: DoctorFix): string {
    const g = f.gates;
    const parts: string[] = [];
    if (g.replay) parts.push(`replay ${g.replay}`);
    if (g.mutants) parts.push(`mutants ${g.mutants.killed}/${g.mutants.n}`);
    if (g.review) parts.push(`review ${g.review}`);
    return parts.join(" · ");
}

interface FixCtx {
    runId: string;
    sourceId: string;
    dir: string | null;
    // The same set readRunDoctor fetches and /api/file serves.
    linked: Set<string>;
}

// ---------- grouping ----------

export interface SkillGroup {
    skill: string;
    cards: DoctorTask[];
    envGroups: { cause: string; tasks: DoctorTask[] }[];
    layerCounts: [string, number][];
    // Distinct PRs with fix among the skill's tasks (countFixPrs).
    fixPrs: number;
    // Tasks with a fix prepared but no PR yet: sorts right after linked PRs.
    prepared: number;
    total: number;
}

// A candidate whose Fix stage never ran (outcome not-run) has no patch and no
// gates: the board leaves it out entirely.
export function fixesForTask(doctor: RunDoctor, taskId: string): DoctorFix[] {
    return (doctor.fixes?.candidates ?? []).filter(
        (c) => c.outcome !== "not-run" && (c.taskId === taskId || c.covers.includes(taskId)),
    );
}

export interface ExistingPrFix {
    pr: DoctorPr;
    // The Fix stage replayed the PR and it flipped the row ("Proposed PR").
    verified: boolean;
    // The diagnosis's reason for naming this PR.
    basis: string;
    // The replay's evidence, when there was a check, and the PR head it used.
    evidence: string | null;
    headSha: string | null;
    // The check could not run: it needs a Windows run.
    needsWindows: boolean;
}

/**
 * Someone else's open PR that fixes this row, from the doctor's structured
 * fields only (no reading of prose): the diagnosis says `fixed_by_pr` with
 * verdict "fixes", the PR is open in the row's existing PRs, and the Fix
 * stage's pr_checks entry for the row (same number) is verified (counts as a
 * PR with fix) or untested / absent (shown as "may fix this", not counted).
 * not_verified, a malformed check, may_help and unrelated show nothing.
 */
export function existingPrFix(doctor: RunDoctor, t: DoctorTask): ExistingPrFix | null {
    const f = t.fixedByPr;
    if (!f || f.verdict !== "fixes") return null;
    const pr = t.existingPrs.find((p) => p.state === "open" && p.url.endsWith(`/pull/${f.number}`));
    if (!pr) return null;
    const c = own(doctor.fixes?.prChecks, t.taskId);
    if (c === null) return null; // an entry that did not validate
    const check = c && c.number === f.number ? c : null;
    if (check?.verdict === "not_verified") return null;
    return {
        pr,
        verified: check?.verdict === "verified",
        basis: f.basis,
        evidence: check?.evidence || null,
        headSha: check?.headSha ?? null,
        needsWindows: check?.reason === "needs-windows-run",
    };
}

export interface TaskFixes {
    // The doctor's own PR: opened for this task or for a pr-ready candidate
    // that covers it (opened.json is keyed by the candidate's primary task),
    // else the triage's doctor_pr. "PR with fix".
    ownPr: string | null;
    // The pr-ready candidates behind it, for their gate verdicts.
    ready: DoctorFix[];
    // pr-ready candidates with no PR opened yet: "Fix prepared, PR not
    // opened yet". Not a PR with fix.
    prepared: DoctorFix[];
    // Someone else's open PR for this row: verified ("Proposed PR"; a PR with
    // fix) or not ("Open PR may fix this", not counted). Never the doctor's
    // own PR.
    existingPr: ExistingPrFix | null;
    // Other fix candidates (needs-live-run, ...).
    others: DoctorFix[];
    // One key per distinct PR with fix (own or verified existing), for counting.
    keys: string[];
    // Card sort tier, lower first (see fixTier).
    tier: number;
}

function openedFor(doctor: RunDoctor, taskId: string): string | null {
    return own(doctor.opened, taskId) ?? null;
}

// Own PR, then a verified proposed PR, then a prepared fix, then an
// unverified open PR, then other candidates, then the rest.
function fixTier(f: Omit<TaskFixes, "tier" | "keys">): number {
    if (f.ownPr) return 0;
    if (f.existingPr?.verified) return 1;
    if (f.prepared.length) return 2;
    if (f.existingPr) return 3;
    if (f.others.length) return 4;
    return 5;
}

export function taskFixes(doctor: RunDoctor, t: DoctorTask): TaskFixes {
    const cands = fixesForTask(doctor, t.taskId);
    const ready = cands.filter(isReady);
    // URLs here are allowlisted by the parsers (parseOpened, parseTriage).
    const ownPr =
        openedFor(doctor, t.taskId) ??
        ready.map((c) => openedFor(doctor, c.taskId)).find((u) => u !== null) ??
        t.doctorPr;
    const ext = existingPrFix(doctor, t);
    const base = {
        ownPr,
        ready,
        prepared: ownPr ? [] : ready,
        existingPr: ext && ext.pr.url !== ownPr ? ext : null,
        others: cands.filter((c) => !isReady(c)),
    };
    const keys = [
        ...(ownPr ? [ownPr] : []),
        ...(base.existingPr?.verified ? [base.existingPr.pr.url] : []),
    ];
    return { ...base, keys, tier: fixTier(base) };
}

/** Tasks with a fix prepared but no PR opened (sort tie-break only). */
export function countPrepared(doctor: RunDoctor, tasks: DoctorTask[]): number {
    return tasks.filter((t) => taskFixes(doctor, t).prepared.length > 0).length;
}

/** Distinct PRs with fix (the doctor's own or a verified proposed one) over these tasks. */
export function countFixPrs(doctor: RunDoctor, tasks: DoctorTask[]): number {
    return new Set(tasks.flatMap((t) => taskFixes(doctor, t).keys)).size;
}

const prsWithFix = (n: number) => `${n} PR${n === 1 ? "" : "s"} with fix`;

/**
 * Visible failures by skill, as the doctor's page groups them. Skills with the
 * most PRs with fix come first, then the most prepared fixes; ties go to more
 * failed tasks, then name.
 */
export function groupBySkill(
    doctor: RunDoctor,
    active: Set<string> | null,
    query: string,
): SkillGroup[] {
    const q = query.trim().toLowerCase();
    const vis = (doctor.triage?.tasks ?? []).filter(
        (t) =>
            (!active || active.has(t.layer)) &&
            (!q || JSON.stringify(t).toLowerCase().includes(q)),
    );
    const by = new Map<string, DoctorTask[]>();
    for (const t of vis) {
        const k = t.skill || "(no skill)";
        by.set(k, [...(by.get(k) ?? []), t]);
    }
    return [...by.keys()]
        .map((skill) => {
            const items = by.get(skill)!;
            const groups = new Map<string, DoctorTask[]>();
            const cards: DoctorTask[] = [];
            for (const t of items) {
                if (t.envGroup) groups.set(t.envGroup, [...(groups.get(t.envGroup) ?? []), t]);
                else cards.push(t);
            }
            // By fix tier (fixTier), then layer rank and task id.
            const tier = (t: DoctorTask) => taskFixes(doctor, t).tier;
            cards.sort(
                (a, b) =>
                    tier(a) - tier(b) ||
                    rank(b.layer) - rank(a.layer) ||
                    a.taskId.localeCompare(b.taskId),
            );
            return {
                skill,
                cards,
                envGroups: [...groups.entries()].map(([cause, tasks]) => ({ cause, tasks })),
                layerCounts: layerCounts(items),
                fixPrs: countFixPrs(doctor, items),
                prepared: countPrepared(doctor, items),
                total: items.length,
            };
        })
        .sort(
            (a, b) =>
                b.fixPrs - a.fixPrs ||
                b.prepared - a.prepared ||
                b.total - a.total ||
                a.skill.localeCompare(b.skill),
        );
}

// ---------- cards ----------

function Pill({ children, color }: { children: ReactNode; color?: string }) {
    return (
        <span
            className="whitespace-nowrap rounded px-1.5 py-px text-[11px] font-semibold"
            style={
                color
                    ? { color, border: `1px solid ${color}` }
                    : { background: "#eef1f5" }
            }
        >
            {children}
        </span>
    );
}

function TaskId({ id, ctx }: { id: string; ctx: FixCtx }) {
    if (!isLinkableTaskId(id)) return <span className="font-mono break-all">{id}</span>;
    return (
        <Link
            href={withSource(`/runs/${ctx.runId}/${id}`, ctx.sourceId)}
            className="font-mono break-all text-studio-blue hover:underline"
        >
            {id}
        </Link>
    );
}

function TaskCard({ t, doctor, ctx }: { t: DoctorTask; doctor: RunDoctor; ctx: FixCtx }) {
    const layer = layerOf(t.layer);
    const fx = taskFixes(doctor, t);
    const rows: [string, ReactNode, boolean?][] = [];
    if (t.fix) rows.push(["Fix", <RichText key="f" text={t.fix} />]);
    if (t.criterion) rows.push(["Failing check", <RichText key="c" text={t.criterion} />]);
    if (t.evidence) rows.push(["Evidence", <RichText key="e" text={t.evidence} />, true]);
    if (t.passedBefore) rows.push(["Passed before", <RichText key="p" text={t.passedBefore} />]);
    return (
        <article
            className="grid min-w-0 gap-2 rounded-md border border-gray-200 bg-white px-4 py-3"
            style={{ borderLeft: `4px solid ${layer.color}` }}
            data-testid="task-card"
        >
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-sm">
                <TaskId id={t.taskId} ctx={ctx} />
                <Pill color={layer.color}>{layer.label}</Pill>
                {t.solvable && (
                    <Pill>{SOLVABLE.get(t.solvable.split(" ")[0]) ?? t.solvable}</Pill>
                )}
                {t.runStatus && (
                    <Pill>{t.runStatus.replace(/_/g, " ").toLowerCase()}</Pill>
                )}
                {t.confidence && <Pill>{t.confidence} confidence</Pill>}
            </div>
            {t.why && (
                <p className="m-0 max-w-prose text-sm">
                    <RichText text={t.why} />
                </p>
            )}
            <FixRows fx={fx} ctx={ctx} />
            {rows.length > 0 && (
                <dl className="m-0 grid grid-cols-1 gap-x-3.5 gap-y-1 text-xs sm:grid-cols-[max-content_1fr]">
                    {rows.map(([k, v, mono]) => (
                        <div key={k} className="contents">
                            <dt className="text-gray-500">{k}</dt>
                            <dd
                                className={`m-0 min-w-0 [overflow-wrap:anywhere] ${mono ? "font-mono text-[11px] text-gray-600" : ""}`}
                            >
                                {v}
                            </dd>
                        </div>
                    ))}
                </dl>
            )}
        </article>
    );
}

function Gates({ cands }: { cands: DoctorFix[] }) {
    const g = cands.map(gateText).filter(Boolean);
    if (!g.length) return null;
    return <span className="text-[11px] text-gray-600">{g.join("; ")}</span>;
}

// "patch +a/-r", linked to the download only for an id in patchIds.
function Patch({ c, ctx }: { c: DoctorFix; ctx: FixCtx }) {
    if (!c.patchLines) return null;
    const text = `+${c.patchLines.added}/-${c.patchLines.removed}`;
    return (
        <span className="tabular-nums">
            patch{" "}
            {ctx.dir && ctx.linked.has(c.id) ? (
                <a
                    href={patchHref(ctx.runId, ctx.sourceId, ctx.dir, c.id)}
                    className="text-studio-blue hover:underline"
                >
                    {text}
                </a>
            ) : (
                <span>{text}</span>
            )}
        </span>
    );
}

const ROW_BORDER = {
    own: "border-studio-blue",
    proposed: "border-studio-blue/60",
    prepared: "border-dashed border-studio-blue/60",
    candidate: "border-dashed border-gray-300",
} as const;

function FixRow({
    kind,
    testId,
    children,
}: {
    kind: keyof typeof ROW_BORDER;
    testId: string;
    children: ReactNode;
}) {
    return (
        <div
            className={`flex flex-wrap items-baseline gap-x-2 rounded-md border px-3 py-1.5 text-xs ${ROW_BORDER[kind]}`}
            data-testid={testId}
        >
            {children}
        </div>
    );
}

function PrLink({ pr }: { pr: DoctorPr }) {
    return (
        <ExtA href={pr.url} className="font-medium text-studio-blue hover:underline">
            {prLabel(pr.url)} {pr.title}
        </ExtA>
    );
}

function Small({ children }: { children: ReactNode }) {
    return <span className="basis-full text-[11px] text-gray-600">{children}</span>;
}

// The replay's evidence as plain text, with the PR head it ran at.
function Evidence({ text, headSha }: { text: string; headSha: string | null }) {
    return (
        <Small>
            {text}
            {headSha && <span className="ml-1 font-mono text-gray-500">(at {headSha.slice(0, 7)})</span>}
        </Small>
    );
}

// The card's PR rows: "PR with fix" (the doctor's PR), "Proposed PR" (someone
// else's open PR, verified by the doctor's replay), "Fix prepared, PR not
// opened yet", "Open PR may fix this (not verified)", and "Fix candidate:
// <outcome>" for candidates that are not ready.
function FixRows({ fx, ctx }: { fx: TaskFixes; ctx: FixCtx }) {
    const rows: ReactNode[] = [];
    if (fx.ownPr) {
        rows.push(
            <FixRow key="own" kind="own" testId="pr-with-fix">
                <ExtA href={fx.ownPr} className="font-semibold text-studio-blue hover:underline">
                    PR with fix: {prLabel(fx.ownPr)} ↗
                </ExtA>
                <Gates cands={fx.ready} />
            </FixRow>,
        );
    }
    const ext = fx.existingPr;
    if (ext?.verified) {
        const { pr, evidence, headSha } = ext;
        rows.push(
            <FixRow key="proposed" kind="proposed" testId="proposed-pr">
                <b className="text-gray-800">Proposed PR</b>
                <PrLink pr={pr} />
                <span className="text-gray-600">{pr.state}</span>
                {evidence && <Evidence text={evidence} headSha={headSha} />}
            </FixRow>,
        );
    }
    for (const c of fx.prepared) {
        rows.push(
            <FixRow key={c.id} kind="prepared" testId="fix-prepared">
                <span className="text-gray-800">Fix prepared, PR not opened yet</span>
                <Patch c={c} ctx={ctx} />
                <Gates cands={[c]} />
            </FixRow>,
        );
    }
    if (ext && !ext.verified) {
        const { pr, basis, evidence, headSha, needsWindows } = ext;
        rows.push(
            <FixRow key="maybe" kind="candidate" testId="maybe-pr">
                <span className="text-gray-800">Open PR may fix this (not verified)</span>
                <PrLink pr={pr} />
                {needsWindows && <span className="text-gray-600">needs a Windows run</span>}
                {basis && <Small>{basis}</Small>}
                {evidence && <Evidence text={evidence} headSha={headSha} />}
            </FixRow>,
        );
    }
    for (const c of fx.others) {
        rows.push(
            <FixRow key={c.id} kind="candidate" testId="fix-candidate">
                <b className="text-gray-700">Fix candidate: {c.outcome}</b>
                <Patch c={c} ctx={ctx} />
                <Gates cands={[c]} />
            </FixRow>,
        );
    }
    if (rows.length === 0) return null;
    return <div className="grid justify-items-start gap-2">{rows}</div>;
}

function EnvGroup({ cause, tasks, ctx }: { cause: string; tasks: DoctorTask[]; ctx: FixCtx }) {
    const [open, setOpen] = useState(false);
    const first = tasks[0];
    return (
        <div
            className="rounded-md border border-gray-200 bg-white px-4 py-3"
            style={{ borderLeft: `4px solid ${layerOf("env").color}` }}
            data-testid="env-group"
        >
            <button
                type="button"
                aria-expanded={open}
                onClick={() => setOpen((v) => !v)}
                className="text-left text-sm font-semibold"
            >
                {tasks.length} task{tasks.length > 1 ? "s" : ""} stopped at the pre-run check:{" "}
                {cause}
            </button>
            {open && (
                <div className="mt-2 space-y-2 text-sm">
                    {first.why && (
                        <p className="m-0">
                            <RichText text={first.why} />
                        </p>
                    )}
                    {first.fix && (
                        <p className="m-0">
                            <b>Fix:</b> <RichText text={first.fix} />
                        </p>
                    )}
                    <ul className="m-0 list-disc pl-5 text-xs sm:columns-2">
                        {tasks.map((t, i) => (
                            <li key={`${t.taskId}-${i}`}>
                                <TaskId id={t.taskId} ctx={ctx} />
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

function SkillAccordion({
    g,
    open,
    onToggle,
    doctor,
    ctx,
}: {
    g: SkillGroup;
    open: boolean;
    onToggle: () => void;
    doctor: RunDoctor;
    ctx: FixCtx;
}) {
    return (
        <section className="rounded-lg border border-gray-200 bg-white" data-skill={g.skill}>
            <button
                type="button"
                aria-expanded={open}
                onClick={onToggle}
                className={`flex w-full flex-wrap items-center gap-x-3.5 gap-y-1.5 px-4 py-3 text-left hover:bg-gray-50 ${open ? "border-b border-gray-200" : ""}`}
            >
                <span aria-hidden className="text-gray-400">
                    {open ? "▾" : "▸"}
                </span>
                <span className="font-bold">{g.skill}</span>
                <span className="text-sm text-gray-500 tabular-nums">
                    {g.total} failed
                    {g.fixPrs > 0 && ` · ${prsWithFix(g.fixPrs)}`}
                </span>
                <span className="ml-auto flex flex-wrap gap-x-2.5 gap-y-1 text-xs text-gray-500">
                    {g.layerCounts.map(([k, n]) => (
                        <span key={k} title={layerOf(k).label}>
                            <span
                                className="mr-1 inline-block h-2 w-2 rounded-full"
                                style={{ background: layerOf(k).color }}
                            />
                            {n} {layerOf(k).label.toLowerCase()}
                        </span>
                    ))}
                </span>
            </button>
            {open && (
                <div className="grid gap-3 px-4 py-3">
                    {g.cards.map((t, i) => (
                        <TaskCard key={`${t.taskId}-${i}`} t={t} doctor={doctor} ctx={ctx} />
                    ))}
                    {g.envGroups.map((e) => (
                        <EnvGroup key={e.cause} cause={e.cause} tasks={e.tasks} ctx={ctx} />
                    ))}
                </div>
            )}
        </section>
    );
}

// ---------- body ----------

const chip = (pressed: boolean) =>
    `rounded-full border px-3 py-0.5 text-xs ${pressed ? "border-gray-900 bg-gray-900 text-white" : "border-gray-200 bg-white text-gray-800"}`;

function Stats({ triage, counts }: { triage: DoctorTriage; counts: [string, number][] }) {
    const count = (k: string) => counts.find(([l]) => l === k)?.[1] ?? 0;
    const stats: [number | null, string][] = [
        [triage.rowsRun, "rows run"],
        [triage.tasks.length, "rows listed"],
        [count("env"), "environment"],
        [triage.tasks.filter(isFixable).length, "fixable in the repo"],
        [count("agent"), "agent mistakes"],
    ];
    return (
        <div className="flex flex-wrap gap-x-7 gap-y-2.5 tabular-nums" data-testid="doctor-stats">
            {stats
                .filter(([v]) => v !== null)
                .map(([v, l]) => (
                    <div key={l}>
                        <b className="block text-xl leading-tight">{v}</b>
                        <span className="text-xs text-gray-500">{l}</span>
                    </div>
                ))}
        </div>
    );
}

function LayerBar({ counts, n }: { counts: [string, number][]; n: number }) {
    if (n === 0) return null;
    return (
        <div>
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500">
                Failures by layer
            </div>
            <div
                className="flex h-3.5 overflow-hidden rounded-sm bg-gray-100"
                role="img"
                aria-label="Failures by layer"
            >
                {counts.map(([k, c]) => (
                    <i
                        key={k}
                        className="block h-full"
                        style={{ width: `${(c / n) * 100}%`, background: layerOf(k).color }}
                        title={`${layerOf(k).label}: ${c}`}
                    />
                ))}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-gray-500">
                {counts.map(([k, c]) => (
                    <span key={k}>
                        <span
                            className="mr-1.5 inline-block h-2.5 w-2.5 rounded-sm align-[-1px]"
                            style={{ background: layerOf(k).color }}
                        />
                        {layerOf(k).label} {c}
                    </span>
                ))}
            </div>
        </div>
    );
}

function FixFirst({ doctor, triage }: { doctor: RunDoctor; triage: DoctorTriage }) {
    if (triage.actions.length === 0) return null;
    return (
        <section className="rounded-lg border border-gray-200 px-5 py-4" aria-label="Fix first">
            <h3 className="text-base font-semibold text-gray-900">Fix first</h3>
            <ol className="mt-2.5 grid list-decimal gap-2 pl-5 text-sm">
                {triage.actions.map((a, i) => {
                    const fixPrs = countFixPrs(
                        doctor,
                        triage.tasks.filter((t) => a.taskIds.includes(t.taskId)),
                    );
                    return (
                        <li key={i} className="pl-1">
                            <span className="font-bold tabular-nums text-studio-blue">{a.count}</span>
                            {" · "}
                            <RichText text={a.text} />
                            {fixPrs > 0 && (
                                <span
                                    className="ml-2 whitespace-nowrap rounded bg-studio-blue/10 px-1.5 py-px text-[11px] font-semibold text-studio-blue"
                                    data-testid="action-ready"
                                >
                                    {prsWithFix(fixPrs)}
                                </span>
                            )}
                        </li>
                    );
                })}
            </ol>
        </section>
    );
}

function SkillFailures({
    doctor,
    present,
    ctx,
}: {
    doctor: RunDoctor;
    present: string[];
    ctx: FixCtx;
}) {
    // null: every layer shown (all chips pressed), as on the doctor's page.
    const [active, setActive] = useState<Set<string> | null>(null);
    const [query, setQuery] = useState("");
    const [openSkills, setOpenSkills] = useState<Set<string>>(new Set());
    const groups = groupBySkill(doctor, active, query);
    const clickChip = (k: string) => {
        setActive((cur) => {
            if (!cur) return new Set([k]);
            const next = new Set(cur);
            if (next.has(k)) next.delete(k);
            else next.add(k);
            return next.size === 0 || present.every((p) => next.has(p)) ? null : next;
        });
    };
    return (
        <section className="space-y-2.5" aria-label="Skill failures">
            <h3 className="text-lg font-semibold text-gray-900">Skill failures</h3>
            <div className="flex flex-wrap gap-2">
                <button
                    type="button"
                    className={chip(false)}
                    onClick={() => setOpenSkills(new Set(groups.map((g) => g.skill)))}
                >
                    Expand all
                </button>
                <button type="button" className={chip(false)} onClick={() => setOpenSkills(new Set())}>
                    Collapse all
                </button>
            </div>
            <div
                className="flex flex-wrap items-center gap-2 border-b border-gray-200 py-2"
                role="toolbar"
                aria-label="Filter failures"
            >
                <span className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">
                    Show
                </span>
                {present.map((k) => {
                    const pressed = !active || active.has(k);
                    return (
                        <button
                            key={k}
                            type="button"
                            aria-pressed={pressed}
                            className={chip(pressed)}
                            onClick={() => clickChip(k)}
                        >
                            {layerOf(k).label}
                        </button>
                    );
                })}
                <input
                    type="search"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search task, cause, PR…"
                    aria-label="Search failures"
                    className="min-w-0 max-w-xs flex-1 rounded-md border border-gray-200 px-2.5 py-1 text-sm"
                />
            </div>
            <div className="grid gap-2.5">
                {groups.length === 0 ? (
                    <p className="text-sm text-gray-500">No failures match this filter.</p>
                ) : (
                    groups.map((g) => (
                        <SkillAccordion
                            key={g.skill}
                            g={g}
                            // A search opens every match, as on the doctor's page.
                            open={openSkills.has(g.skill) || query.trim() !== ""}
                            onToggle={() =>
                                setOpenSkills((cur) => {
                                    const next = new Set(cur);
                                    if (next.has(g.skill)) next.delete(g.skill);
                                    else next.add(g.skill);
                                    return next;
                                })
                            }
                            doctor={doctor}
                            ctx={ctx}
                        />
                    ))
                )}
            </div>
        </section>
    );
}

function Body({ doctor, ctx }: { doctor: RunDoctor; ctx: FixCtx }) {
    const triage = doctor.triage!;
    const counts = useMemo(() => layerCounts(triage.tasks), [triage.tasks]);
    return (
        <div className="space-y-5 rounded-b-lg bg-white px-5 pb-4 pt-3">
            <Stats triage={triage} counts={counts} />
            <LayerBar counts={counts} n={triage.tasks.length} />
            <FixFirst doctor={doctor} triage={triage} />
            <SkillFailures doctor={doctor} present={counts.map(([k]) => k)} ctx={ctx} />
        </div>
    );
}

// ---------- block ----------

function BuildLinks({ status }: { status: DoctorStatus }) {
    return (
        <>
            {status.buildUrl && (
                <>
                    {" · "}
                    <BuildLink href={status.buildUrl} label="build" />
                </>
            )}
            {status.trigger && (
                <>
                    {" · "}
                    <BuildLink href={status.trigger} label="nightly" />
                </>
            )}
        </>
    );
}

/**
 * The Task failure analysis panel: one bordered section, collapsed by default.
 * Open, it shows the AI summary's top, the doctor's report, then the rest of
 * the AI summary (Findings down) under a collapsed disclosure that lists its
 * section headings. Only a done report with a triage has a report body; any
 * other state shows the whole AI summary and the doctor's state.
 */
export function TaskFailureAnalysis({
    doctor,
    runId,
    sourceId,
    summary = null,
    rest = null,
}: {
    doctor: RunDoctor;
    runId: string;
    sourceId: string;
    summary?: string | null;
    rest?: string | null;
}) {
    const [open, setOpen] = useState(false);
    const [restOpen, setRestOpen] = useState(false);
    const hasBody = doctor.status.state === "done" && doctor.triage !== null;
    const expandable = hasBody || !!summary;
    const ctx: FixCtx = {
        runId,
        sourceId,
        dir: doctor.status.fixesDir,
        linked: patchIds(doctor.fixes),
    };
    const label = (
        <span className="text-xs font-semibold text-studio-blue uppercase tracking-wide">
            Task failure analysis
        </span>
    );
    const line = (
        <span className="text-xs text-gray-600" data-testid="doctor-digest">
            {digest(doctor)}
        </span>
    );
    const headings = rest ? sectionHeadings(rest) : [];
    return (
        <section
            className="border border-studio-blue/30 bg-studio-blue/5 rounded-lg"
            aria-label="Task failure analysis"
        >
            {expandable ? (
                <button
                    type="button"
                    onClick={() => setOpen((v) => !v)}
                    aria-expanded={open}
                    className="w-full px-4 py-3 flex flex-wrap items-center justify-between gap-3 text-left hover:bg-studio-blue/10 rounded-lg transition-colors"
                >
                    <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                        {label}
                        {line}
                    </span>
                    <span className="text-xs text-studio-blue whitespace-nowrap">
                        {open ? "Hide" : "Read analysis →"}
                    </span>
                </button>
            ) : (
                <div className="px-4 py-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    {label}
                    {line}
                    <span className="text-xs text-gray-600">
                        <BuildLinks status={doctor.status} />
                    </span>
                </div>
            )}
            {expandable && open && (
                <>
                    {summary && (
                        <div className="border-t border-studio-blue/20 bg-white px-5 py-4">
                            <SummaryMarkdown markdown={summary} />
                        </div>
                    )}
                    {hasBody ? (
                        <>
                            <div className="border-t border-studio-blue/20 bg-white px-5 pt-3 text-xs text-gray-500">
                                skill-doctor
                                {doctor.fixes?.fixMode && ` · fix mode ${doctor.fixes.fixMode}`}
                                <BuildLinks status={doctor.status} />
                            </div>
                            <Body doctor={doctor} ctx={ctx} />
                        </>
                    ) : (
                        <div className="border-t border-studio-blue/20 bg-white px-5 py-3 text-xs text-gray-600">
                            skill-doctor: {digest(doctor)}
                            <BuildLinks status={doctor.status} />
                        </div>
                    )}
                    {rest && (
                        <div className="border-t border-studio-blue/20 bg-white px-5 py-3 rounded-b-lg">
                            <button
                                type="button"
                                onClick={() => setRestOpen((v) => !v)}
                                aria-expanded={restOpen}
                                className="w-full text-left text-xs text-studio-blue"
                            >
                                {restOpen ? "▾" : "▸"} More from the AI summary
                                {headings.length > 0 && ` · ${headings.join(" · ")}`}
                            </button>
                            {restOpen && (
                                <div className="pt-3">
                                    <SummaryMarkdown markdown={rest} />
                                </div>
                            )}
                        </div>
                    )}
                </>
            )}
        </section>
    );
}
