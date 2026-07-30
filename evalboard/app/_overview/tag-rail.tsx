import Link from "next/link";
import type { TagCount } from "@/lib/overview";
import type { Window } from "@/lib/reviews-types";

type Variant = "neutral" | "rose" | "indigo";

const STYLES: Record<
    Variant,
    { chip: string; chipActive: string; count: string; countActive: string }
> = {
    neutral: {
        chip: "bg-gray-50 text-gray-700 border-gray-200 hover:bg-gray-100",
        chipActive: "bg-gray-800 text-white border-gray-800",
        count: "text-gray-400",
        countActive: "text-gray-300",
    },
    rose: {
        chip: "bg-rose-50 text-rose-700 border-rose-200 hover:bg-rose-100",
        chipActive: "bg-rose-600 text-white border-rose-600",
        count: "text-rose-400",
        countActive: "text-rose-200",
    },
    // Indigo for skills — the primary grouping axis. Picked to sit between
    // gray (generic tags) and rose (review tags) in visual weight.
    indigo: {
        chip: "bg-indigo-50 text-indigo-700 border-indigo-200 hover:bg-indigo-100 font-medium",
        chipActive: "bg-indigo-600 text-white border-indigo-600 font-medium",
        count: "text-indigo-400",
        countActive: "text-indigo-200",
    },
};

function hrefForTag(
    basePath: string,
    tag: string | null,
    window: Window | null,
    q: string | null,
    harness: string | null,
): string {
    const params = new URLSearchParams();
    if (window) params.set("window", window);
    if (tag) params.set("tag", tag);
    if (q) params.set("q", q);
    // Preserve the active harness scope across tag clicks. null is the
    // all-harness default and is expressed as the param's absence, so there is
    // nothing to carry. Without this, filtering by a tag would silently widen a
    // codex/antigravity view back out to every harness.
    if (harness) params.set("h", harness);
    const qs = params.toString();
    return qs ? `${basePath}?${qs}` : basePath;
}

function TagChip({
    tag,
    count,
    variant,
    active,
    basePath,
    window,
    q,
    harness,
}: {
    tag: string;
    count: number;
    variant: Variant;
    active: boolean;
    basePath: string;
    window: Window | null;
    q: string | null;
    harness: string | null;
}) {
    const s = STYLES[variant];
    return (
        <Link
            href={hrefForTag(basePath, active ? null : tag, window, q, harness)}
            scroll={false}
            className={`inline-flex items-center gap-1 text-[11px] leading-none px-2 py-1 rounded border transition-colors ${active ? s.chipActive : s.chip}`}
        >
            <span>{tag}</span>
            <span
                className={`tabular-nums ${active ? s.countActive : s.count}`}
            >
                {count}
            </span>
        </Link>
    );
}

// Pick the top-N tags to show, sticking the active tag at the end if it would
// otherwise be hidden so the user can always see (and clear) the current filter.
function pickShown(
    tags: TagCount[],
    limit: number,
    activeTag: string | null,
): { shown: TagCount[]; remaining: number } {
    const top = tags.slice(0, limit);
    const appendedActive =
        !!activeTag &&
        tags.some((t) => t.tag === activeTag) &&
        !top.some((t) => t.tag === activeTag);
    const shown = appendedActive
        ? [...top, tags.find((t) => t.tag === activeTag)!]
        : top;
    const remaining = Math.max(
        0,
        tags.length - limit - (appendedActive ? 1 : 0),
    );
    return { shown, remaining };
}

// Single wrapped row with all three tag groups rendered as differently-colored
// chips. Color carries the section meaning (indigo=skill, rose=review,
// gray=other), so explicit labels are dropped.
export function MergedTagRail({
    skills,
    taskTags,
    reviewTags,
    activeTag,
    basePath = "/",
    window = null,
    q = null,
    harness = null,
    limit = 24,
}: {
    skills: TagCount[];
    taskTags: TagCount[];
    reviewTags: TagCount[];
    activeTag: string | null;
    // Path the chip links point at — "/" for the overview, "/trends" for the
    // trends page. Query string is preserved.
    basePath?: string;
    // Null on pages that don't expose a window selector (e.g. /trends).
    window?: Window | null;
    q?: string | null;
    // Active harness scope to preserve in chip links (null = not harness-scoped).
    harness?: string | null;
    limit?: number;
}) {
    const s = pickShown(skills, limit, activeTag);
    const r = pickShown(reviewTags, limit, activeTag);
    const t = pickShown(taskTags, limit, activeTag);
    const totalRemaining = s.remaining + r.remaining + t.remaining;
    const isEmpty =
        s.shown.length === 0 && r.shown.length === 0 && t.shown.length === 0;
    if (isEmpty) {
        return <span className="text-xs text-gray-400">no tags</span>;
    }
    return (
        <div className="flex flex-wrap gap-1.5">
            {s.shown.map((tc) => (
                <TagChip
                    key={`s:${tc.tag}`}
                    tag={tc.tag}
                    count={tc.count}
                    variant="indigo"
                    active={tc.tag === activeTag}
                    basePath={basePath}
                    window={window}
                    q={q}
                    harness={harness}
                />
            ))}
            {r.shown.map((tc) => (
                <TagChip
                    key={`r:${tc.tag}`}
                    tag={tc.tag}
                    count={tc.count}
                    variant="rose"
                    active={tc.tag === activeTag}
                    basePath={basePath}
                    window={window}
                    q={q}
                    harness={harness}
                />
            ))}
            {t.shown.map((tc) => (
                <TagChip
                    key={`t:${tc.tag}`}
                    tag={tc.tag}
                    count={tc.count}
                    variant="neutral"
                    active={tc.tag === activeTag}
                    basePath={basePath}
                    window={window}
                    q={q}
                    harness={harness}
                />
            ))}
            {totalRemaining > 0 && (
                <span className="inline-flex items-center text-[11px] leading-none px-2 py-1 rounded border border-dashed border-gray-300 text-gray-500">
                    +{totalRemaining} more
                </span>
            )}
        </div>
    );
}

// Tiny legend strip explaining what the chip colors mean. Placed above
// the chip rail so users don't have to hover-discover the convention.
export function ChipLegend() {
    const entries: Array<[string, string, string]> = [
        ["skill", "bg-indigo-400", "skill"],
        ["review", "bg-rose-400", "review"],
        ["tag", "bg-gray-400", "tag"],
    ];
    return (
        <div className="flex items-center gap-3 text-[10px] text-gray-500">
            {entries.map(([key, dotCls, label]) => (
                <span key={key} className="inline-flex items-center gap-1">
                    <span
                        className={`inline-block w-2 h-2 rounded-full ${dotCls}`}
                    />
                    {label}
                </span>
            ))}
        </div>
    );
}
