import Link from "next/link";
import type { TagCount } from "@/lib/overview";
import type { Window } from "@/lib/reviews-types";

type Variant = "neutral" | "rose";

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
};

function hrefForTag(
    tag: string | null,
    window: Window,
    q: string | null,
): string {
    const params = new URLSearchParams();
    params.set("window", window);
    if (tag) params.set("tag", tag);
    if (q) params.set("q", q);
    return `/?${params.toString()}`;
}

function TagChip({
    tag,
    count,
    variant,
    active,
    window,
    q,
}: {
    tag: string;
    count: number;
    variant: Variant;
    active: boolean;
    window: Window;
    q: string | null;
}) {
    const s = STYLES[variant];
    return (
        <Link
            href={hrefForTag(active ? null : tag, window, q)}
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

export function TagRail({
    label,
    tags,
    variant,
    activeTag,
    window,
    q = null,
    limit = 12,
}: {
    label: string;
    tags: TagCount[];
    variant: Variant;
    activeTag: string | null;
    window: Window;
    q?: string | null;
    limit?: number;
}) {
    // Show top `limit`, plus the active tag if it would otherwise be hidden,
    // so the user can always see (and unset) the current filter.
    const top = tags.slice(0, limit);
    const appendedActive =
        !!activeTag &&
        tags.some((t) => t.tag === activeTag) &&
        !top.some((t) => t.tag === activeTag);
    const shown = appendedActive
        ? [...top, tags.find((t) => t.tag === activeTag)!]
        : top;
    // `+N more` reflects tags hidden by the top-N cap, independent of whether
    // we appended the active tag (which was already outside the cap).
    const remaining = Math.max(0, tags.length - limit - (appendedActive ? 1 : 0));
    return (
        <div className="flex items-baseline gap-3 flex-wrap">
            <span className="text-xs text-gray-500 shrink-0 w-24">
                {label}
            </span>
            {shown.length === 0 ? (
                <span className="text-xs text-gray-400">none</span>
            ) : (
                <div className="flex flex-wrap gap-1.5">
                    {shown.map((t) => (
                        <TagChip
                            key={t.tag}
                            tag={t.tag}
                            count={t.count}
                            variant={variant}
                            active={t.tag === activeTag}
                            window={window}
                            q={q}
                        />
                    ))}
                    {remaining > 0 && (
                        <span className="inline-flex items-center text-[11px] leading-none px-2 py-1 rounded border border-dashed border-gray-300 text-gray-500">
                            +{remaining} more
                        </span>
                    )}
                </div>
            )}
        </div>
    );
}
