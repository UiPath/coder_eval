// Turn *display*. Turns are shown as a plain count on task views; they are no
// longer an efficiency signal. That is wall-clock seconds now, see lib/timing.ts.

// Number to display in any "Turns" cell: the visible-events count, one per tool
// call plus one for the final reply. Mirrors the Turn timeline body, so the cell
// equals the rows an expanded section would show. SDK num_turns (totalTurns) is
// not consulted — it counts assistant messages, which can bundle tool_use with
// trailing text — but stays in the data layer for cost/debug.
export function displayedTurns(
    actualCommands: number | null,
    hasFinalReply: boolean,
): number | null {
    if (actualCommands == null) return hasFinalReply ? 1 : null;
    return actualCommands + (hasFinalReply ? 1 : 0);
}

export function fmtTurnsCount(n: number | null): string {
    return n == null ? "—" : `${n}`;
}
