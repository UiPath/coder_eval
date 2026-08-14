import { statusCategory } from "./status";

// Shown on the green "Mature" status pill for a task that was skipped this run
// (5 consecutive passes → re-validated only on its weekly slot) and carried
// forward as a pass. Explains why a passing row was not executed this run.
export const MATURE_TOOLTIP =
    "Mature: skipped this run to save cost and carried forward as a pass " +
    "(re-validated about weekly on its fixed slot). It was not executed this run.";

// Shown on a mature task's id when it DOES link out — to the most recent run
// where the task actually executed (the clickable case; see TaskIdCell). Names
// that run so it's clear the link leaves the current run.
export function matureLinkTooltip(sourceRunLabel: string): string {
    return (
        "Mature: skipped this run (carried forward as a pass). " +
        `Last ran in run ${sourceRunLabel} — opens that execution.`
    );
}

// Aggregate-voice variant, for a windowed count of mature appearances rather
// than one run's row. MATURE_TOOLTIP says "this run" twice, which is right on a
// single-run surface and wrong on a page that renders no single run at all.
export function matureAggregateTooltip(
    matureSkips: number,
    appearances: number,
): string {
    return (
        `${matureSkips} of these ${appearances} appearances were mature ` +
        "carry-forwards: skipped to save cost and carried forward as a pass " +
        "(re-validated about weekly on a fixed slot), not executed."
    );
}

// Fallback tooltip when no recent execution was found within the look-back
// window, so the id stays non-clickable.
export const MATURE_NO_SOURCE_TOOLTIP =
    "Mature: skipped this run (carried forward as a pass). " +
    "No recent execution is available to open.";

export function MaturePill() {
    // Same green as a passing StatusPill — a mature task is a carried-forward
    // pass, so it reads as green everywhere; only the label and the help cursor
    // distinguish it (hover explains why it has no clickable detail).
    return (
        <span
            title={MATURE_TOOLTIP}
            className="inline-flex items-center whitespace-nowrap px-3 py-1 text-xs rounded-full border bg-green-50 text-green-700 border-green-200 cursor-help"
        >
            Mature
        </span>
    );
}

export function StatusPill({
    status,
    relabel = false,
}: {
    status: string | null;
    relabel?: boolean;
}) {
    const ok = status === "SUCCESS" || status === "Completed";
    // Colour EVERY non-passing terminal status red, not just the enumerated
    // few. statusCategory maps all coder_eval failure statuses (FAILURE, ERROR,
    // TIMEOUT, MAX_TURNS_EXHAUSTED, TOKEN_BUDGET_EXCEEDED, …) to failed/error;
    // this catches the ones that previously fell through to a misleading grey.
    // Flow-execution failures (Faulted/Failed) land in statusCategory's "failed"
    // bucket too. Only null/unknown stays grey.
    const cat = statusCategory(status);
    const isFailure = !ok && (cat === "failed" || cat === "error");
    // Narrower list drives the relabel-to-"Failed" text so specific statuses
    // (e.g. MAX_TURNS_EXHAUSTED) keep their raw label while still showing red.
    const fail =
        status === "FAILURE" ||
        status === "ERROR" ||
        status === "BUILD_FAILED" ||
        status === "TIMEOUT" ||
        status === "Faulted" ||
        status === "Failed";
    const cls = ok
        ? "bg-green-50 text-green-700 border-green-200"
        : isFailure
          ? "bg-red-50 text-red-700 border-red-200"
          : "bg-gray-50 text-gray-600 border-gray-200";
    const raw = status ?? "—";
    const label =
        relabel && ok
            ? "Passed"
            : relabel && status === "TIMEOUT"
              ? "Timed out"
              : relabel && fail
                ? "Failed"
                : raw;
    return (
        <span
            className={`inline-flex items-center whitespace-nowrap px-3 py-1 text-xs rounded-full border ${cls}`}
        >
            {label}
        </span>
    );
}
