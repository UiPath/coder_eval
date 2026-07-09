import Link from "next/link";
import type { ActivationScore } from "@/lib/runs";

function pct(v: number): string {
    return `${(v * 100).toFixed(0)}%`;
}

// One metric card in the run-page grid (same shape as the Failed / cost / time
// cards), but clickable — it links to the dedicated /runs/<id>/activation page
// where the per-skill breakdown and every case live. Activation is a separate
// signal from the skills pass-rate, so it sits alongside the others, not folded
// into them.
export function ActivationCard({
    runId,
    activation,
}: {
    runId: string;
    activation: ActivationScore;
}) {
    return (
        <Link
            href={`/runs/${runId}/activation`}
            className="block bg-white border border-gray-200 rounded-lg p-4 hover:border-studio-blue hover:bg-gray-50 transition-colors"
        >
            <div className="text-xs text-gray-500 uppercase tracking-wide">
                Activation
            </div>
            <div className="text-2xl font-semibold mt-1 tabular-nums text-gray-900">
                {pct(activation.score)}
            </div>
            <div className="text-xs text-studio-blue mt-0.5">
                {activation.nSkillsSampled}/{activation.denominator} skills · view
                →
            </div>
        </Link>
    );
}
