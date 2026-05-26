import {
    fmtTurnsCount,
    tintForRatio,
    turnRatio,
    turnsCellClasses,
} from "@/lib/turns";

export function TurnsStat({
    turns,
    expectedTurns,
}: {
    turns: number | null;
    expectedTurns: number | null;
}) {
    const tint = tintForRatio(turnRatio(turns, expectedTurns));
    return (
        <div>
            <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Turns
            </dt>
            <dd
                className={`mt-0.5 tabular-nums font-medium ${turnsCellClasses(tint)}`}
                title={
                    expectedTurns != null
                        ? `expected_turns target: ${expectedTurns}`
                        : "no expected_turns target set"
                }
            >
                {fmtTurnsCount(turns)}
            </dd>
        </div>
    );
}

export function ExpectedTurnsStat({
    expectedTurns,
}: {
    expectedTurns: number | null;
}) {
    return (
        <div>
            <dt className="text-xs text-gray-500 uppercase tracking-wide">
                Expected turns
            </dt>
            <dd className="text-gray-900 font-medium mt-0.5 tabular-nums">
                {fmtTurnsCount(expectedTurns)}
            </dd>
        </div>
    );
}

