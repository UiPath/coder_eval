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
        status === "Faulted" ||
        status === "Failed";
    const cls = ok
        ? "bg-green-50 text-green-700 border-green-200"
        : fail
          ? "bg-red-50 text-red-700 border-red-200"
          : "bg-gray-50 text-gray-600 border-gray-200";
    const raw = status ?? "—";
    const label = relabel && ok ? "Passed" : relabel && fail ? "Failed" : raw;
    return (
        <span
            className={`inline-block px-2.5 py-0.5 text-xs rounded-full border ${cls}`}
        >
            {label}
        </span>
    );
}
