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

// Pure turn-efficiency ratio (turns ÷ expected_turns), used to tint per-task
// "Turns" cells. This is deliberately blind to pass/fail: the cell answers
// "was this task's turn usage efficient?", which is meaningful regardless of
// outcome — a task that crashed at 2 turns should NOT read as "over budget"
// red in the Turns column. The aggregate headline metric is the opposite: see
// withinTurnBudget below, which `overview.ts::turnBudgetRateForTasks` treats as
// over budget for any *budgeted* non-SUCCESS task. The two intentionally
// diverge (the cell ignores outcome; the headline folds it in).
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
    // Collapsed replicate rows carry an averaged (fractional) turn count, e.g.
    // 15.333…; cap at 2 decimals and drop trailing zeros so whole counts still
    // render as "7" (not "7.00") and a half stays "6.5".
    return n == null ? "—" : `${Number(n.toFixed(2))}`;
}

// Fail a task's turn-budget check once its visible turns exceed the budget by
// more than this fraction (> 1.5× expected_turns).
export const TURN_BUDGET_TOLERANCE = 0.5;

// Whether a task stayed within (1 + tolerance) × its expected-turns budget,
// using the documented visible-turn count. Returns null when the task is not
// eligible: no visible-turn count, or no positive expected_turns budget.
export function withinTurnBudget(
    visibleTurns: number | null,
    expectedTurns: number | null,
    tolerance: number = TURN_BUDGET_TOLERANCE,
): boolean | null {
    if (visibleTurns == null || expectedTurns == null || expectedTurns < 1) {
        return null;
    }
    return visibleTurns <= expectedTurns * (1 + tolerance);
}
