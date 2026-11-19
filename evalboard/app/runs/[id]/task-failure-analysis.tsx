import Link from "next/link";
import type { ReactNode } from "react";
import { withSource } from "@/app/_lib/source-param";
import { fmtTimestamp, fmtUsd, humanizeTaskId } from "@/lib/format";
import {
    DOCTOR_DIR,
    isAdoUrl,
    isLinkableTaskId,
    isSkillsPrUrl,
    patchIds,
    type DoctorFix,
    type DoctorStatus,
    type DoctorTask,
    type RunDoctor,
} from "@/lib/doctor";

// The skill-doctor report for one run, drawn natively from its JSON (never
// the doctor's own page.html). Every string in it is plain text; the only
// links are to this board's task pages and to URLs of an exact known shape.

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

function prNumber(url: string): string {
    const m = /\/pull\/(\d+)$/.exec(url);
    return m ? `#${m[1]}` : url;
}

function Counts({
    label,
    counts,
}: {
    label: string;
    counts: Record<string, number>;
}) {
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (entries.length === 0) return null;
    return (
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="text-gray-500 w-12">{label}</span>
            {entries.map(([k, n]) => (
                <span
                    key={k}
                    className="rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 tabular-nums"
                >
                    {k} <span className="font-semibold">{n}</span>
                </span>
            ))}
        </div>
    );
}

/** One line under the AI summary when the full panel is not shown. */
export function DoctorStatusLine({ status }: { status: DoctorStatus }) {
    const build = status.buildUrl ? (
        <>
            {" · "}
            <ExternalLink href={status.buildUrl} ok={isAdoUrl}>
                build
            </ExternalLink>
        </>
    ) : null;
    let body: ReactNode;
    switch (status.state) {
        case "queued":
        case "running":
            body = (
                <>
                    {status.state}
                    {status.startedAt &&
                        ` since ${fmtTimestamp(status.startedAt)}`}
                    {build}
                </>
            );
            break;
        case "failed":
        case "skipped":
            body = (
                <>
                    {status.state}
                    {status.reason && `: ${status.reason}`}
                    {build}
                </>
            );
            break;
        case "retracted":
            body = <>the report for this run was retracted</>;
            break;
        case "done":
            body = (
                <>
                    report done, but its triage file is missing or did not
                    parse
                    {build}
                </>
            );
            break;
    }
    return (
        <p
            className="text-xs text-gray-500"
            data-testid="doctor-status-line"
        >
            <span className="font-semibold">skill-doctor:</span> {body}
        </p>
    );
}

function TaskRow({
    t,
    runId,
    sourceId,
}: {
    t: DoctorTask;
    runId: string;
    sourceId: string;
}) {
    return (
        <tr className="border-t border-gray-100 align-top">
            <td className="py-1.5 pr-3 font-medium">
                {isLinkableTaskId(t.taskId) ? (
                    <Link
                        href={withSource(`/runs/${runId}/${t.taskId}`, sourceId)}
                        className="text-studio-blue hover:underline"
                    >
                        {humanizeTaskId(t.taskId)}
                    </Link>
                ) : (
                    t.taskId
                )}
                {t.state && t.state !== "new" && (
                    <span className="ml-1 text-[10px] text-amber-700">
                        {t.state}
                        {t.nightsSeen > 1 && `, ${t.nightsSeen} nights`}
                    </span>
                )}
            </td>
            <td className="py-1.5 pr-3 text-gray-600">{t.skill}</td>
            <td className="py-1.5 pr-3">
                {t.layer}
                {t.cls && (
                    <div className="text-[10px] text-gray-500">{t.cls}</div>
                )}
            </td>
            <td className="py-1.5 pr-3 text-gray-600">{t.confidence ?? "—"}</td>
            <td className="py-1.5 pr-3 whitespace-pre-wrap">{t.why ?? "—"}</td>
            <td className="py-1.5 pr-3 whitespace-pre-wrap">{t.fix ?? "—"}</td>
            <td className="py-1.5 pr-3">
                {t.existingPrs.length === 0
                    ? "—"
                    : t.existingPrs.map((p, i) => (
                          <div key={`${p.url}-${i}`} title={p.title}>
                              <ExternalLink href={p.url} ok={isSkillsPrUrl}>
                                  {isSkillsPrUrl(p.url) ? prNumber(p.url) : p.url}
                              </ExternalLink>{" "}
                              <span className="text-[10px] text-gray-500">
                                  {p.state}
                              </span>
                          </div>
                      ))}
            </td>
            <td className="py-1.5 text-gray-600">
                {t.owners.length ? t.owners.join(", ") : "—"}
            </td>
        </tr>
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

function PatchCell({ fix, href }: { fix: DoctorFix; href: string | null }) {
    if (!fix.patchLines) return <>—</>;
    const text = `+${fix.patchLines.added}/-${fix.patchLines.removed}`;
    if (!href) return <>{text}</>;
    return (
        <a href={href} className="text-studio-blue hover:underline">
            {text}
        </a>
    );
}

function FixesTable({
    doctor,
    runId,
    sourceId,
}: {
    doctor: RunDoctor;
    runId: string;
    sourceId: string;
}) {
    const fixes = doctor.fixes;
    const dir = doctor.status.fixesDir;
    if (!fixes || !dir) return null;
    // The same set readRunDoctor fetches and /api/file serves.
    const linked = patchIds(fixes);
    return (
        <div className="space-y-1.5">
            <h3 className="text-sm font-semibold text-gray-900">
                Fixes
                <span className="ml-2 text-xs font-normal text-gray-500">
                    {fixes.fixMode && `mode ${fixes.fixMode} · `}
                    {fmtUsd(fixes.costUsd)}
                    {fixes.totalCapUsd != null &&
                        ` of ${fmtUsd(fixes.totalCapUsd)} cap`}
                </span>
            </h3>
            {fixes.candidates.length === 0 ? (
                <p className="text-xs text-gray-500">No fix candidates.</p>
            ) : (
                <table className="w-full text-xs">
                    <thead className="text-left text-gray-500">
                        <tr>
                            <th className="pr-3 font-semibold">Outcome</th>
                            <th className="pr-3 font-semibold">Task</th>
                            <th className="pr-3 font-semibold">Rows</th>
                            <th className="pr-3 font-semibold">Patch</th>
                            <th className="pr-3 font-semibold">Gates</th>
                            <th className="font-semibold">Cost</th>
                        </tr>
                    </thead>
                    <tbody>
                        {fixes.candidates.map((f) => (
                            <tr
                                key={f.id}
                                className="border-t border-gray-100 align-top"
                            >
                                <td className="py-1.5 pr-3 font-medium">
                                    {f.outcome}
                                    {f.title && (
                                        <div className="text-[10px] font-normal text-gray-600">
                                            {f.title}
                                        </div>
                                    )}
                                </td>
                                <td className="py-1.5 pr-3">
                                    {humanizeTaskId(f.taskId)}
                                </td>
                                <td className="py-1.5 pr-3 tabular-nums">
                                    {f.covers.length}
                                </td>
                                <td className="py-1.5 pr-3 tabular-nums">
                                    <PatchCell
                                        fix={f}
                                        href={
                                            linked.has(f.id)
                                                ? withSource(
                                                      `/api/file?run=${encodeURIComponent(runId)}&path=${encodeURIComponent(`${DOCTOR_DIR}/${dir}/${f.id}.patch`)}`,
                                                      sourceId,
                                                  )
                                                : null
                                        }
                                    />
                                </td>
                                <td className="py-1.5 pr-3">{gateText(f)}</td>
                                <td className="py-1.5 tabular-nums">
                                    {fmtUsd(f.costUsd)}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </div>
    );
}

export function TaskFailureAnalysis({
    doctor,
    runId,
    sourceId,
}: {
    doctor: RunDoctor;
    runId: string;
    sourceId: string;
}) {
    const { status, triage } = doctor;
    if (!triage) return null;
    // Per-task triage state (new / changed / known), counted for the header.
    const byState: Record<string, number> = {};
    for (const t of triage.tasks) {
        if (t.state) byState[t.state] = (byState[t.state] ?? 0) + 1;
    }
    const vh = triage.summary.vsHistory;
    return (
        <section
            className="border border-gray-200 rounded-lg bg-white px-4 py-3 space-y-4"
            aria-label="Task Failure Analysis"
        >
            <div className="space-y-1.5">
                <div className="flex flex-wrap items-baseline gap-2">
                    <h2 className="text-sm font-semibold text-gray-900">
                        Task Failure Analysis
                    </h2>
                    <span className="text-xs text-gray-500">
                        skill-doctor · {triage.summary.rowsFailed} failed,{" "}
                        {triage.summary.rowsDiagnosed} diagnosed · diagnosis{" "}
                        {fmtUsd(status.costUsd ?? triage.costUsd)}
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
                    </span>
                </div>
                <Counts label="layer" counts={triage.summary.byLayer} />
                <Counts label="class" counts={triage.summary.byClass} />
                <Counts label="state" counts={byState} />
                {vh && (
                    <p className="text-xs text-gray-500">
                        vs {vh.baseRun}: {vh.new} new, {vh.persisting}{" "}
                        persisting, {vh.fixed} fixed
                    </p>
                )}
                {triage.disclaimer && (
                    <p className="text-xs italic text-gray-500">
                        {triage.disclaimer}
                    </p>
                )}
            </div>

            {triage.actions.length > 0 && (
                <div className="space-y-1">
                    <h3 className="text-sm font-semibold text-gray-900">
                        Actions
                    </h3>
                    <ul className="list-disc pl-5 text-xs space-y-0.5">
                        {triage.actions.map((a, i) => (
                            <li key={i}>
                                <span className="text-gray-500">[{a.kind}]</span>{" "}
                                {a.text}
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {/* Collapsed by default: a nightly can list 80+ rows, and the
                task grid below should stay in reach. */}
            <details className="overflow-x-auto">
                <summary className="cursor-pointer text-sm font-semibold text-gray-900">
                    Tasks ({triage.tasks.length})
                </summary>
                <table className="mt-1.5 w-full text-xs">
                    <thead className="text-left text-gray-500">
                        <tr>
                            <th className="pr-3 font-semibold">Task</th>
                            <th className="pr-3 font-semibold">Skill</th>
                            <th className="pr-3 font-semibold">Layer</th>
                            <th className="pr-3 font-semibold">Confidence</th>
                            <th className="pr-3 font-semibold">Why</th>
                            <th className="pr-3 font-semibold">Fix</th>
                            <th className="pr-3 font-semibold">Existing PRs</th>
                            <th className="font-semibold">Owners</th>
                        </tr>
                    </thead>
                    <tbody>
                        {triage.tasks.map((t, i) => (
                            <TaskRow
                                key={`${t.taskId}-${i}`}
                                t={t}
                                runId={runId}
                                sourceId={sourceId}
                            />
                        ))}
                    </tbody>
                </table>
            </details>

            <FixesTable doctor={doctor} runId={runId} sourceId={sourceId} />
        </section>
    );
}
