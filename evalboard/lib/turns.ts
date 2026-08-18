// Turn *display*. Turns are still shown as a plain count on task views; they are
// no longer an efficiency signal.
//
// The budget helpers that used to live here compared a turn count against a
// hand-written `run_limits.expected_turns`. That signal was retired: the numbers
// were never maintained, a third of the suite carried none, and a turn is not a
// unit of time (seconds per visible turn ranged p10 5.2s to p90 15.5s, max 156s
// — a Read and a 20-minute deploy both counted 1). Efficiency is measured in
// seconds now; see lib/timing.ts.

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

export function fmtTurnsCount(n: number | null): string {
    return n == null ? "—" : `${n}`;
}
