// Shown in place of the status pill for a "mature" task that was skipped this
// run (5 consecutive passes → re-validated only on its weekly slot) and carried
// forward as a pass. The title explains why the row has no clickable detail.
export const MATURE_TOOLTIP =
    "Mature: skipped this run to save cost (re-validated about weekly). It was not executed, " +
    "so there's no per-task detail to open — carried forward as a pass.";

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
    const fail =
        status === "FAILURE" ||
        status === "ERROR" ||
        status === "TIMEOUT" ||
        status === "Faulted" ||
        status === "Failed";
    const cls = ok
        ? "bg-green-50 text-green-700 border-green-200"
        : fail
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
