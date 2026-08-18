import { fmtTurnsCount } from "@/lib/turns";
import {
    expectedTimeTitle,
    fmtTaskSeconds,
    fmtTimeRatio,
    timeCellClasses,
    timeRatio,
    tintForTimeRatio,
} from "@/lib/timing";

// Duration against the time this task is expected to need. Tinted, because this
// is where efficiency is read now; the ratio is spelled out in the hover so the
// color is never the only signal.
export function DurationStat({
    durationSeconds,
    expectedSeconds,
}: {
    durationSeconds: number | null;
    expectedSeconds: number | null;
}) {
    const ratio = timeRatio(durationSeconds, expectedSeconds);
    return (
        <div>
            <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Duration
            </dt>
            <dd
                className={`mt-0.5 tabular-nums font-medium ${timeCellClasses(tintForTimeRatio(ratio))}`}
                title={
                    ratio != null
                        ? `${fmtTimeRatio(ratio)} · ${expectedTimeTitle(expectedSeconds)}`
                        : expectedTimeTitle(expectedSeconds)
                }
            >
                {fmtTaskSeconds(durationSeconds)}
            </dd>
        </div>
    );
}

// The derived line itself, so a reader can see what the tint was measured
// against without hovering. Never hand-written: the eval runner derives it per
// harness from that task's own passing history and stamps it into run.json.
export function ExpectedTimeStat({
    expectedSeconds,
}: {
    expectedSeconds: number | null;
}) {
    return (
        <div>
            <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Expected time
            </dt>
            <dd
                className="text-gray-900 font-medium mt-0.5 tabular-nums"
                title={expectedTimeTitle(expectedSeconds)}
            >
                {expectedSeconds != null ? fmtTaskSeconds(expectedSeconds) : "—"}
            </dd>
        </div>
    );
}

// Plain turn count. Turns are still worth seeing on a task; they are no longer
// scored against a budget (see lib/turns.ts).
export function TurnsStat({ turns }: { turns: number | null }) {
    return (
        <div>
            <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Turns
            </dt>
            <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                {fmtTurnsCount(turns)}
            </dd>
        </div>
    );
}
