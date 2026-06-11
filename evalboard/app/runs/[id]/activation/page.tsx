import { notFound } from "next/navigation";
import Link from "next/link";
import {
    readActivationScore,
    readActivationTasks,
    type ActivationCaseRow,
    type ActivationSkillScore,
} from "@/lib/runs";

export const dynamic = "force-dynamic";

function pct(v: number | null): string {
    if (v == null) return "—";
    return `${(v * 100).toFixed(0)}%`;
}

// A case is a mistake when the skill that fired isn't the one expected: a miss
// (nothing fired), a false positive (something fired on a negative), or the wrong
// skill. Tied to the two columns the table shows, not to the run status (which
// also flips on harness noise like MAX_TURNS_EXHAUSTED). A negative case expects
// nothing — normalised to "". Unknown (no triggered signal) is never flagged.
function isMistake(c: ActivationCaseRow): boolean {
    if (c.triggeredSkill == null) return false;
    return (c.expectedSkill ?? "") !== c.triggeredSkill;
}

// Display a skill cell: the skill name, or "none" for an empty string (no skill
// expected / nothing fired), or "—" when the signal is unknown.
function skillLabel(s: string | null): string {
    if (s == null) return "—";
    return s === "" ? "none" : s;
}

// Prompts cell: how many positive prompts this skill ran, amber when it fell short
// of the sampling bar (those skills count as 0 in the score regardless of recall).
function coverage(s: ActivationSkillScore): { text: string; cls: string } {
    if (s.nPrompts === 0) return { text: "none", cls: "text-gray-400" };
    if (!s.sampled) return { text: `${s.nPrompts}`, cls: "text-amber-700 font-medium" };
    return { text: `${s.nPrompts}`, cls: "text-gray-700" };
}

function StatCard({
    label,
    value,
    sub,
    valueClass = "text-gray-900",
}: {
    label: string;
    value: string;
    sub?: string;
    valueClass?: string;
}) {
    return (
        <div className="bg-white border border-gray-200 rounded-lg p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">
                {label}
            </div>
            <div
                className={`text-2xl font-semibold mt-1 tabular-nums ${valueClass}`}
            >
                {value}
            </div>
            {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
        </div>
    );
}

export default async function ActivationPage({
    params,
}: {
    params: Promise<{ id: string }>;
}) {
    const { id } = await params;
    const [activation, cases] = await Promise.all([
        // Both read the nested activation sub-run (run_id/activation/run.json):
        // the rollup and the per-case list. Kept entirely out of the skills run.
        readActivationScore(id),
        readActivationTasks(id),
    ]);
    if (!activation) notFound();

    // Per-skill: every catalog skill, coverage gaps (0-contribution) first.
    const perSkill = [...activation.perSkill].sort(
        (a, b) =>
            a.contributes - b.contributes || a.skill.localeCompare(b.skill),
    );
    // Individual cases: all the negatives first ("" = no skill expected), then the
    // positives in alphabetical order by expected skill, then by prompt.
    const caseRows = (cases ?? []).slice().sort((a, b) => {
        const aNeg = a.expectedSkill === "" ? 0 : 1;
        const bNeg = b.expectedSkill === "" ? 0 : 1;
        return (
            aNeg - bNeg ||
            (a.expectedSkill ?? "~").localeCompare(b.expectedSkill ?? "~") ||
            (a.prompt ?? a.taskId).localeCompare(b.prompt ?? b.taskId)
        );
    });
    const gaps = activation.denominator - activation.nSkillsSampled;
    const wrongCount = caseRows.filter(isMistake).length;

    return (
        <div className="space-y-6">
            <div className="space-y-1">
                <Link
                    href={`/runs/${id}`}
                    className="text-xs text-studio-blue hover:underline"
                >
                    ← back to run
                </Link>
                <h1 className="text-xl font-semibold text-gray-900">
                    Skill activation
                </h1>
                <div className="text-xs text-gray-500 font-mono">{id}</div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <StatCard
                    label="Activation score"
                    value={pct(activation.score)}
                    sub={`mean recall over all ${activation.denominator} skills — uncovered ones count as 0`}
                />
                <StatCard
                    label="Skills sampled"
                    value={`${activation.nSkillsSampled} / ${activation.denominator}`}
                    sub={`ran ≥ ${activation.minPrompts} prompts; the rest score 0`}
                />
                <StatCard
                    label="Coverage gaps"
                    value={String(gaps)}
                    sub={`skills counted as 0 (under ${activation.minPrompts} prompts)`}
                    valueClass={gaps > 0 ? "text-amber-700" : "text-gray-900"}
                />
                <StatCard
                    label="Cases run"
                    value={String(activation.nCases ?? caseRows.length)}
                    sub={wrongCount > 0 ? `${wrongCount} wrong` : "all correct"}
                    valueClass="text-gray-900"
                />
            </div>

            <section className="space-y-2">
                <h2 className="text-sm font-semibold text-gray-900">
                    Per skill ({perSkill.length})
                </h2>
                <p className="text-xs text-gray-500">
                    Every skill in the catalog. The score is the mean of the
                    recall column over all {activation.denominator} skills — a
                    skill that ran fewer than {activation.minPrompts} prompts (or
                    none) is counted as 0, so coverage gaps drag the mean down.
                </p>
                <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-200">
                                <th className="px-4 py-2 font-medium">Skill</th>
                                <th className="px-4 py-2 font-medium text-right">
                                    Prompts
                                </th>
                                <th className="px-4 py-2 font-medium text-right">
                                    Recall (yes)
                                </th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {perSkill.map((s) => {
                                const cov = coverage(s);
                                return (
                                    <tr key={s.skill} className="hover:bg-gray-50">
                                        <td className="px-4 py-2 font-mono text-gray-800">
                                            {s.skill}
                                        </td>
                                        <td
                                            className={`px-4 py-2 text-right tabular-nums ${cov.cls}`}
                                        >
                                            {cov.text}
                                        </td>
                                        <td
                                            className={`px-4 py-2 text-right tabular-nums ${
                                                s.sampled
                                                    ? "text-gray-700"
                                                    : "text-amber-700"
                                            }`}
                                        >
                                            {s.sampled
                                                ? pct(s.recallYes)
                                                : `${pct(s.recallYes)} → 0`}
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            </section>

            <section className="space-y-2">
                <h2 className="text-sm font-semibold text-gray-900">
                    Cases ({caseRows.length})
                </h2>
                <p className="text-xs text-gray-500">
                    Every prompt that ran this nightly: the skill it should activate
                    and the skill that actually fired. Rows in red are mistakes —
                    a miss, a false positive, or the wrong skill. Click a prompt to
                    inspect its trace.
                </p>
                <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-200">
                                <th className="px-4 py-2 font-medium">Prompt</th>
                                <th className="px-4 py-2 font-medium">
                                    Expected skill
                                </th>
                                <th className="px-4 py-2 font-medium">
                                    Triggered skill
                                </th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {caseRows.map((t) => {
                                const mistake = isMistake(t);
                                return (
                                    <tr
                                        key={t.taskId}
                                        className={
                                            mistake
                                                ? "bg-red-50 hover:bg-red-100"
                                                : "hover:bg-gray-50"
                                        }
                                    >
                                        <td className="px-4 py-2 max-w-xl">
                                            <Link
                                                href={`/runs/${id}/${t.taskId}`}
                                                className={`hover:underline ${
                                                    mistake
                                                        ? "text-red-800"
                                                        : "text-gray-800 hover:text-studio-blue"
                                                }`}
                                            >
                                                {t.prompt ?? (
                                                    <span className="font-mono text-xs">
                                                        {t.taskId
                                                            .split("/")
                                                            .pop()}
                                                    </span>
                                                )}
                                            </Link>
                                        </td>
                                        <td className="px-4 py-2 font-mono text-gray-600">
                                            {skillLabel(t.expectedSkill)}
                                        </td>
                                        <td
                                            className={`px-4 py-2 font-mono ${
                                                mistake
                                                    ? "text-red-800 font-medium"
                                                    : "text-gray-600"
                                            }`}
                                        >
                                            {skillLabel(t.triggeredSkill)}
                                        </td>
                                    </tr>
                                );
                            })}
                            {caseRows.length === 0 && (
                                <tr>
                                    <td
                                        colSpan={3}
                                        className="px-4 py-6 text-center text-sm text-gray-500"
                                    >
                                        no activation cases in this run
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </section>
        </div>
    );
}
