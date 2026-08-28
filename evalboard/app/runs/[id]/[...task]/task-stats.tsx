import {
    expectedTimeTitle,
    fmtTaskSeconds,
    fmtTimeRatio,
    fmtTimeRatioCell,
    timeCellClasses,
    timeRatio,
    tintForTimeRatio,
} from "@/lib/timing";

// Duration against the time this task is expected to need. The ratio is printed
// beside the time rather than left to a hover, so the color is never the only
// signal that a task ran slow.
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
                className="mt-0.5 tabular-nums font-medium text-gray-900"
                title={
                    ratio != null
                        ? `${fmtTimeRatio(ratio)} · ${expectedTimeTitle(expectedSeconds)}`
                        : expectedTimeTitle(expectedSeconds)
                }
            >
                {fmtTaskSeconds(durationSeconds)}
                {ratio != null && (
                    <span
                        className={`ml-1.5 text-xs ${timeCellClasses(tintForTimeRatio(ratio))}`}
                    >
                        {fmtTimeRatioCell(ratio)}
                    </span>
                )}
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
