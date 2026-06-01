"use client";

// Tokens ↔ USD unit toggle for token columns. When "usd" is active the columns
// show a dollar value priced from list rates (lib/pricing.ts) — which is an
// approximation, not the authoritative SDK cost — so an "estimated" badge is
// shown alongside. Shared by the run-page task grid and the task-page message
// timeline so the control looks and behaves the same on both.
export type Unit = "tokens" | "usd";

export function UnitToggle({
    value,
    onChange,
    className,
}: {
    value: Unit;
    onChange: (u: Unit) => void;
    className?: string;
}) {
    return (
        <span className={"inline-flex items-center gap-2 " + (className ?? "")}>
            {/* Badge sits to the LEFT of the control so the toggle buttons stay
                anchored to the right edge — toggling won't shift them around. */}
            {value === "usd" && (
                <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-700">
                    estimated
                </span>
            )}
            <span
                className="inline-flex overflow-hidden rounded-md border border-gray-200 text-xs"
                role="group"
                aria-label="Token unit"
            >
                {(["tokens", "usd"] as Unit[]).map((u) => (
                    <button
                        key={u}
                        type="button"
                        aria-pressed={value === u}
                        onClick={() => onChange(u)}
                        className={`px-2 py-0.5 transition-colors ${
                            value === u
                                ? "bg-studio-blue text-white"
                                : "bg-white text-gray-600 hover:bg-gray-50"
                        }`}
                    >
                        {u === "tokens" ? "Tokens" : "USD"}
                    </button>
                ))}
            </span>
        </span>
    );
}
