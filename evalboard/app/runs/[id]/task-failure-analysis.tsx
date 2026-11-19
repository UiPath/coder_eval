"use client";

import Link from "next/link";
import { useMemo, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { withSource } from "@/app/_lib/source-param";
import { fmtTimestamp, fmtUsd } from "@/lib/format";
import {
    DOCTOR_DIR,
    isAdoUrl,
    isLinkableTaskId,
    isSkillsPrUrl,
    patchIds,
    type DoctorFix,
    type DoctorPr,
    type DoctorStatus,
    type DoctorTask,
    type RunDoctor,
} from "@/lib/doctor";

// The skill-doctor report for one run, drawn natively from its JSON in the
// order of the doctor's own page (stats → failures by layer → Fix first →
// skill failures), never by embedding that page. Every string is plain text;
// the only links are this board's task pages and URLs of an exact known shape.

// Layer order, labels and colors, as on the doctor's page.
export const LAYERS: Record<string, { label: string; color: string }> = {
    env: { label: "Environment", color: "#56708f" },
    grader: { label: "Grader", color: "#b07410" },
    task: { label: "Task setup", color: "#7a4fb0" },
    agent: { label: "Agent mistake", color: "#c2413a" },
    skill: { label: "Skill", color: "#d0661c" },
    upstream: { label: "Upstream (CLI)", color: "#2f6fd0" },
    flaky: { label: "Flaky", color: "#7d8794" },
    undiagnosed: { label: "Not diagnosed", color: "#9aa5b3" },
};
const ORDER = Object.keys(LAYERS);
const UNKNOWN_LAYER = { label: "Other", color: "#9aa5b3" };
const layerOf = (k: string) => LAYERS[k] ?? { ...UNKNOWN_LAYER, label: k };
const rank = (k: string) => {
    const i = ORDER.indexOf(k);
    return i === -1 ? ORDER.length : i;
};

const SOLVABLE: Record<string, string> = {
    "env-fix": "Fix the tenant",
    "yes-grader": "Fixable: grader",
    "yes-task": "Fixable: task",
    "yes-skill": "Fixable: skill",
    "upstream-report": "Upstream report",
    no: "Not a repo fix",
};
const isFixable = (t: DoctorTask) => /^yes/.test(t.solvable ?? "");
const isReady = (c: DoctorFix) => c.outcome === "pr-ready";

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
                    <a
                        key={`${i}-${j}`}
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-studio-blue hover:underline"
                    >
                        {url}
                    </a>,
                    seg.slice(url.length),
                );
            } else out.push(seg);
        });
    });
    return <>{out}</>;
}

function ExternalLink({
    href,
    ok,
    children,
}: {
    href: string | null;
    ok: (u: unknown) => boolean;
    children?: ReactNode;
}) {
    if (!href) return null;
    if (!ok(href)) return <span>{children ?? href}</span>;
    return (
        <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-studio-blue hover:underline"
        >
            {children ?? href}
        </a>
    );
}

function prLabel(url: string): string {
    const m = /\/pull\/(\d+)$/.exec(url);
    return m ? `#${m[1]}` : url;
}

function PrList({ prs }: { prs: DoctorPr[] }) {
    return (
        <>
            {prs.map((p, i) => (
                <span key={`${p.url}-${i}`}>
                    {i > 0 && "; "}
                    <ExternalLink href={p.url} ok={isSkillsPrUrl}>
                        {isSkillsPrUrl(p.url) ? prLabel(p.url) : p.url}
                    </ExternalLink>
                    {p.title && ` ${p.title}`}
                    <span className="text-gray-500"> ({p.state})</span>
                </span>
            ))}
        </>
    );
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
            if (!triage) return "report done, but its triage file is missing or did not parse";
            const n = countFixPrs(doctor, triage.tasks);
            const errors = `${triage.rowsFailed} error${triage.rowsFailed === 1 ? "" : "s"}`;
            return n ? `${errors} · ${prsWithFix(n)}` : errors;
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
    return parts.length ? parts.join(" · ") : "—";
}

interface FixCtx {
    runId: string;
    sourceId: string;
    dir: string | null;
    // The same set readRunDoctor fetches and /api/file serves.
    linked: Set<string>;
}

function PatchCell({ fix, ctx }: { fix: DoctorFix; ctx: FixCtx }) {
    if (!fix.patchLines) return <>—</>;
    const text = `+${fix.patchLines.added}/-${fix.patchLines.removed}`;
    if (!ctx.dir || !ctx.linked.has(fix.id)) return <span>{text}</span>;
    return (
        <a
            href={patchHref(ctx.runId, ctx.sourceId, ctx.dir, fix.id)}
            className="text-studio-blue hover:underline"
        >
            {text}
        </a>
    );
}

// ---------- grouping ----------

export interface SkillGroup {
    skill: string;
    cards: DoctorTask[];
    envGroups: { cause: string; tasks: DoctorTask[] }[];
    layerCounts: [string, number][];
    // Distinct PRs with a fix among the skill's tasks (countFixPrs).
    ready: number;
    total: number;
}

export function fixesForTask(doctor: RunDoctor, taskId: string): DoctorFix[] {
    return (doctor.fixes?.candidates ?? []).filter(
        (c) => c.taskId === taskId || c.covers.includes(taskId),
    );
}

// "#N" or ".../pull/N" in a task's fix text.
const PR_REF_RE = /(?:#|\/pull\/)(\d+)\b/g;

/**
 * A third-party proposed PR: the task's fix text names it AND it is one of the
 * task's existing PRs, open, with an allowlisted URL. Merged or unknown PRs
 * do not count.
 */
export function proposedPrs(t: DoctorTask): DoctorPr[] {
    if (!t.fix) return [];
    const named = new Set([...t.fix.matchAll(PR_REF_RE)].map((m) => m[1]));
    return t.existingPrs.filter((p) => {
        if (p.state !== "open" || !isSkillsPrUrl(p.url)) return false;
        return named.has(/(\d+)$/.exec(p.url)![1]);
    });
}

export interface TaskFixes {
    // The doctor's own fix: its opened PR (allowlisted), else its prepared
    // pr-ready candidates. Both render as "PR with fix".
    doctorPr: string | null;
    ready: DoctorFix[];
    // Third-party open PRs the fix text points at ("Proposed PR").
    proposed: DoctorPr[];
    // Other fix candidates (needs-live-run, ...).
    others: DoctorFix[];
    // One key per distinct PR with a fix, for counting across tasks.
    keys: string[];
    // Card sort: own fix, then proposed PR, then other candidates, then rest.
    tier: number;
}

export function taskFixes(doctor: RunDoctor, t: DoctorTask): TaskFixes {
    const cands = fixesForTask(doctor, t.taskId);
    const doctorPr = isSkillsPrUrl(t.doctorPr) ? t.doctorPr : null;
    const ready = doctorPr ? [] : cands.filter(isReady);
    const others = cands.filter((c) => !isReady(c));
    const proposed = proposedPrs(t);
    const keys = [
        ...(doctorPr ? [`pr:${doctorPr}`] : ready.map((c) => `fix:${c.id}`)),
        ...proposed.map((p) => `pr:${p.url}`),
    ];
    const tier = doctorPr || ready.length ? 0 : proposed.length ? 1 : others.length ? 2 : 3;
    return { doctorPr, ready, proposed, others, keys, tier };
}

/** Distinct PRs with a fix (own or proposed) over these tasks. */
export function countFixPrs(doctor: RunDoctor, tasks: DoctorTask[]): number {
    return new Set(tasks.flatMap((t) => taskFixes(doctor, t).keys)).size;
}

const prsWithFix = (n: number) => `${n} PR${n === 1 ? "" : "s"} with fix`;

/**
 * Visible failures by skill, as the doctor's page groups them. Skills with the
 * most PRs with fix come first; ties go to more failed tasks, then name.
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
            // The doctor's own fix first, then a proposed PR, then other fix
            // candidates, then the rest; within each, layer rank and task id.
            const tier = (t: DoctorTask) => taskFixes(doctor, t).tier;
            cards.sort(
                (a, b) =>
                    tier(a) - tier(b) ||
                    rank(b.layer) - rank(a.layer) ||
                    a.taskId.localeCompare(b.taskId),
            );
            const lc = new Map<string, number>();
            for (const t of items) lc.set(t.layer, (lc.get(t.layer) ?? 0) + 1);
            return {
                skill,
                cards,
                envGroups: [...groups.entries()].map(([cause, tasks]) => ({ cause, tasks })),
                layerCounts: [...lc.entries()].sort((a, b) => rank(a[0]) - rank(b[0])),
                ready: countFixPrs(doctor, items),
                total: items.length,
            };
        })
        .sort(
            (a, b) =>
                b.ready - a.ready || b.total - a.total || a.skill.localeCompare(b.skill),
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
    if (t.existingPrs.length) rows.push(["Open PRs on these files", <PrList key="o" prs={t.existingPrs} />]);
    if (t.relatedSkillPrs.length)
        rows.push(["Related skill PRs", <PrList key="s" prs={t.relatedSkillPrs} />]);
    if (t.related) rows.push(["Related", <RichText key="r" text={t.related} />]);
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
                    <Pill>{SOLVABLE[t.solvable.split(" ")[0]] ?? t.solvable}</Pill>
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
            <FixRows t={t} fx={fx} doctor={doctor} ctx={ctx} />
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

// The prepared PR body is model-written markdown: no raw HTML (ReactMarkdown
// drops it by default), links only for allowlisted URLs, images as alt text.
const BODY_COMPONENTS: Components = {
    a: ({ href, children }) =>
        isSkillsPrUrl(href) || isAdoUrl(href) ? (
            <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
            </a>
        ) : (
            <span>{children}</span>
        ),
    img: ({ alt }) => <span>{alt ?? ""}</span>,
};

const BOX = "rounded-md border px-3 py-1.5 text-xs";

function FixMeta({ c, ctx }: { c: DoctorFix; ctx: FixCtx }) {
    return (
        <>
            {c.patchLines && (
                <span className="tabular-nums">
                    patch <PatchCell fix={c} ctx={ctx} />
                </span>
            )}
            <span className="text-gray-600">{gateText(c)}</span>
            <span className="tabular-nums text-gray-600">{fmtUsd(c.costUsd)}</span>
        </>
    );
}

/** The doctor's prepared, unopened fix: title expands to the PR body, then the patch. */
function PreparedFix({ c, body, ctx }: { c: DoctorFix; body: string | null; ctx: FixCtx }) {
    const [open, setOpen] = useState(false);
    const title = c.title || c.id;
    return (
        <div className={`${BOX} border-studio-blue`} data-testid="pr-with-fix">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <b className="text-studio-blue">PR with fix</b>
                {body ? (
                    <button
                        type="button"
                        aria-expanded={open}
                        onClick={() => setOpen((v) => !v)}
                        className="text-left font-medium text-gray-900 hover:underline"
                    >
                        {title}
                    </button>
                ) : (
                    <span className="font-medium text-gray-900">{title}</span>
                )}
                <FixMeta c={c} ctx={ctx} />
            </div>
            {open && body && (
                <div className="mt-2 border-t border-gray-200 pt-2">
                    <div className="prose prose-sm max-w-none prose-headings:text-sm" data-testid="pr-body">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={BODY_COMPONENTS}>
                            {body}
                        </ReactMarkdown>
                    </div>
                    {c.patchLines && ctx.dir && ctx.linked.has(c.id) && (
                        <a
                            href={patchHref(ctx.runId, ctx.sourceId, ctx.dir, c.id)}
                            className="mt-1 inline-block font-semibold text-studio-blue hover:underline"
                        >
                            Download patch (+{c.patchLines.added}/-{c.patchLines.removed})
                        </a>
                    )}
                </div>
            )}
        </div>
    );
}

// page.html's "Proposed PR" row, split by who made the fix: "PR with fix"
// (the doctor's opened PR or prepared fix), "Proposed PR" (a third party's open
// PR the fix text names), "Fix candidate: <outcome>" (not ready), or "none yet"
// for a repo-fixable failure.
function FixRows({
    t,
    fx,
    doctor,
    ctx,
}: {
    t: DoctorTask;
    fx: TaskFixes;
    doctor: RunDoctor;
    ctx: FixCtx;
}) {
    const rows: ReactNode[] = [];
    if (fx.doctorPr) {
        rows.push(
            <a
                key="doctor-pr"
                href={fx.doctorPr}
                target="_blank"
                rel="noopener noreferrer"
                className={`${BOX} inline-block border-studio-blue font-semibold text-studio-blue hover:underline`}
                data-testid="pr-with-fix"
            >
                PR with fix: {prLabel(fx.doctorPr)} ↗
            </a>,
        );
    }
    for (const c of fx.ready) {
        rows.push(
            <PreparedFix key={c.id} c={c} body={doctor.fixBodies?.[c.id] ?? null} ctx={ctx} />,
        );
    }
    for (const p of fx.proposed) {
        rows.push(
            <div
                key={p.url}
                className={`${BOX} flex flex-wrap items-baseline gap-x-2 border-studio-blue/60`}
                data-testid="proposed-pr"
            >
                <b className="text-gray-800">Proposed PR</b>
                <a
                    href={p.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-medium text-studio-blue hover:underline"
                >
                    {prLabel(p.url)} {p.title}
                </a>
                <span className="text-gray-600">{p.state}</span>
            </div>,
        );
    }
    for (const c of fx.others) {
        rows.push(
            <div
                key={c.id}
                className={`${BOX} flex flex-wrap items-baseline gap-x-2 border-dashed border-gray-300`}
                data-testid="fix-candidate"
            >
                <b className="text-gray-700">Fix candidate: {c.outcome}</b>
                <FixMeta c={c} ctx={ctx} />
                {c.title && <span className="basis-full text-gray-700">{c.title}</span>}
            </div>,
        );
    }
    if (rows.length === 0 && isFixable(t)) {
        rows.push(
            <span key="none" className={`${BOX} inline-block border-dashed border-gray-300 text-gray-500`}>
                Proposed PR: none yet
            </span>,
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
            style={{ borderLeft: `4px solid ${LAYERS.env.color}` }}
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
                    {g.ready > 0 && ` · ${prsWithFix(g.ready)}`}
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

function Body({ doctor, ctx }: { doctor: RunDoctor; ctx: FixCtx }) {
    const triage = doctor.triage!;
    const tasks = triage.tasks;
    const counts = useMemo(() => {
        const m = new Map<string, number>();
        for (const t of tasks) m.set(t.layer, (m.get(t.layer) ?? 0) + 1);
        return [...m.entries()].sort((a, b) => rank(a[0]) - rank(b[0]));
    }, [tasks]);
    const present = counts.map(([k]) => k);
    // null: every layer shown (all chips pressed), as on the doctor's page.
    const [active, setActive] = useState<Set<string> | null>(null);
    const [query, setQuery] = useState("");
    const [openSkills, setOpenSkills] = useState<Set<string>>(new Set());
    const groups = groupBySkill(doctor, active, query);
    const n = tasks.length;
    const count = (k: string) => counts.find(([l]) => l === k)?.[1] ?? 0;
    const stats: [number | null, string][] = [
        [triage.rowsRun, "rows run"],
        [n, "failed rows"],
        [count("env"), "environment"],
        [tasks.filter(isFixable).length, "fixable in the repo"],
        [count("agent"), "agent mistakes"],
    ];
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
        <div className="space-y-5 rounded-b-lg bg-white px-5 pb-4 pt-3">
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

            {n > 0 && (
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
            )}

            {triage.actions.length > 0 && (
                <section className="rounded-lg border border-gray-200 px-5 py-4" aria-label="Fix first">
                    <h3 className="text-base font-semibold text-gray-900">Fix first</h3>
                    <ol className="mt-2.5 grid list-decimal gap-2 pl-5 text-sm">
                        {triage.actions.map((a, i) => {
                            const ready = countFixPrs(
                                doctor,
                                triage.tasks.filter((t) => a.taskIds.includes(t.taskId)),
                            );
                            return (
                                <li key={i} className="pl-1">
                                    <span className="font-bold tabular-nums text-studio-blue">
                                        {a.count}
                                    </span>
                                    {" · "}
                                    <RichText text={a.text} />
                                    {ready > 0 && (
                                        <span
                                            className="ml-2 whitespace-nowrap rounded bg-studio-blue/10 px-1.5 py-px text-[11px] font-semibold text-studio-blue"
                                            data-testid="action-ready"
                                        >
                                            {prsWithFix(ready)}
                                        </span>
                                    )}
                                </li>
                            );
                        })}
                    </ol>
                </section>
            )}

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
                    <ExternalLink href={status.buildUrl} ok={isAdoUrl}>
                        build
                    </ExternalLink>
                </>
            )}
            {status.trigger && (
                <>
                    {" · "}
                    <ExternalLink href={status.trigger} ok={isAdoUrl}>
                        nightly
                    </ExternalLink>
                </>
            )}
        </>
    );
}

/**
 * The Task Failure Analysis block: shaped like the AI summary panel (bordered
 * section, header button, collapsed by default), right under it. The header
 * carries a one-line digest; only a done report with a triage has a body.
 */
export function TaskFailureAnalysis({
    doctor,
    runId,
    sourceId,
}: {
    doctor: RunDoctor;
    runId: string;
    sourceId: string;
}) {
    const [open, setOpen] = useState(false);
    const hasBody = doctor.status.state === "done" && doctor.triage !== null;
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
    return (
        <section
            className="border border-studio-blue/30 bg-studio-blue/5 rounded-lg"
            aria-label="Task failure analysis"
        >
            {hasBody ? (
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
            {hasBody && open && (
                <>
                    <div className="border-t border-studio-blue/20 bg-white px-5 pt-3 text-xs text-gray-500">
                        skill-doctor · diagnosis {fmtUsd(doctor.status.costUsd)}
                        {doctor.fixes && (
                            <>
                                {" · fixes "}
                                {fmtUsd(doctor.fixes.costUsd)}
                                {doctor.fixes.fixMode && ` (${doctor.fixes.fixMode})`}
                            </>
                        )}
                        <BuildLinks status={doctor.status} />
                    </div>
                    <Body doctor={doctor} ctx={ctx} />
                </>
            )}
        </section>
    );
}
