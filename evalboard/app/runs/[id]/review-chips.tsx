"use client";

// Open vocabulary — review tags use a muted rose palette to read as
// "advisory failure-mode metadata" while sharing the same chip shape and
// filter semantics as the standard task-tag row.

const ACTIVE_CLS = "bg-rose-600 text-white border-rose-600";
const INACTIVE_CLS =
    "bg-rose-50 text-rose-700 border-rose-200 hover:bg-rose-100";

// Inline chip rendered next to a task row in the grid. Clickable when
// onToggleTag is supplied (mirrors the standard-tag inline chips).
export function ReviewChips({
    tags,
    title,
    selectedSet,
    onToggleTag,
}: {
    tags: string[];
    title?: string;
    selectedSet?: Set<string>;
    onToggleTag?: (tag: string) => void;
}) {
    if (tags.length === 0) return null;
    return (
        <div className="flex flex-wrap gap-1 mt-0.5">
            {tags.map((tag) => {
                const active = selectedSet?.has(tag) ?? false;
                const cls = active ? ACTIVE_CLS : INACTIVE_CLS;
                const baseCls = `text-[10px] leading-none px-1.5 py-0.5 rounded border transition-colors ${cls}`;
                return onToggleTag ? (
                    <button
                        key={tag}
                        type="button"
                        title={title}
                        onClick={() => onToggleTag(tag)}
                        aria-pressed={active}
                        className={baseCls}
                    >
                        {tag}
                    </button>
                ) : (
                    <span key={tag} title={title} className={baseCls}>
                        {tag}
                    </span>
                );
            })}
        </div>
    );
}

// Filter row rendered below the standard tag filter row. Same shape as the
// standard-tag chip buttons, rose palette, no "x" prefix on the count.
export function ReviewTagFilterRow({
    counts,
    selectedSet,
    onToggleTag,
}: {
    counts: { tag: string; count: number }[];
    selectedSet: Set<string>;
    onToggleTag: (tag: string) => void;
}) {
    if (counts.length === 0) return null;
    const selectedCount = [...selectedSet].filter((t) =>
        counts.some((c) => c.tag === t),
    ).length;
    return (
        <div className="flex flex-wrap items-center gap-1.5 md:flex-1 md:min-w-0">
            <span className="text-xs text-gray-500 mr-1">
                Review tags{selectedCount > 1 ? " (all match)" : ""}:
            </span>
            {counts.map(({ tag, count }) => {
                const active = selectedSet.has(tag);
                const cls = active ? ACTIVE_CLS : INACTIVE_CLS;
                return (
                    <button
                        key={tag}
                        type="button"
                        onClick={() => onToggleTag(tag)}
                        aria-pressed={active}
                        className={`text-xs px-2 py-0.5 rounded border transition-colors ${cls}`}
                    >
                        {tag}
                        <span
                            className={`ml-1 tabular-nums ${active ? "text-white/80" : "text-rose-400"}`}
                        >
                            {count}
                        </span>
                    </button>
                );
            })}
        </div>
    );
}
