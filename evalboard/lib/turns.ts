export interface TurnRatioThresholds {
    yellow: number;
    red: number;
}

export function getTurnRatioThresholds(): TurnRatioThresholds {
    const parse = (raw: string | undefined, fallback: number) => {
        if (!raw) return fallback;
        const n = Number(raw);
        return Number.isFinite(n) && n > 0 ? n : fallback;
    };
    return {
        yellow: parse(process.env.EVALBOARD_TURNS_YELLOW_RATIO, 1.25),
        red: parse(process.env.EVALBOARD_TURNS_RED_RATIO, 1.5),
    };
}

export type TurnTint = "green" | "yellow" | "red" | null;

export function turnRatio(
    totalTurns: number | null,
    expectedTurns: number | null,
): number | null {
    if (totalTurns == null || expectedTurns == null || expectedTurns <= 0) return null;
    return totalTurns / expectedTurns;
}

// Number to display in any "Turns" cell — the visible-events count, i.e.
// one per tool call plus one for the final reply when present. Mirrors
// the Turn timeline body so the cell number equals the number of rows
// the user would see if they expanded the section. SDK num_turns
// (totalTurns) is intentionally not consulted: it counts assistant
// messages, which can bundle tool_use + trailing text into a single
// turn, drifting from the visible event count on some conversations.
// totalTurns is kept in the data layer for cost/debug surfacing.
//
// See docs/features/2026-05-22-visible-turns.md for the full rationale.
export function displayedTurns(
    actualCommands: number | null,
    hasFinalReply: boolean,
): number | null {
    if (actualCommands == null) return hasFinalReply ? 1 : null;
    return actualCommands + (hasFinalReply ? 1 : 0);
}

export function tintForRatio(
    ratio: number | null,
    t: TurnRatioThresholds = getTurnRatioThresholds(),
): TurnTint {
    if (ratio == null) return null;
    if (ratio > t.red) return "red";
    if (ratio > t.yellow) return "yellow";
    return "green";
}

export function turnsCellClasses(tint: TurnTint): string {
    switch (tint) {
        case "green":
            return "text-emerald-700";
        case "yellow":
            return "text-amber-700";
        case "red":
            return "text-rose-700";
        default:
            return "text-gray-900";
    }
}

export function fmtTurnsCount(n: number | null): string {
    return n == null ? "—" : `${n}`;
}
