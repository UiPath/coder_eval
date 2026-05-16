"use client";

// Shared chip primitive for the run-detail page. Used in both the top
// filter rail (size="md", with count badge) and per-task rows in the grid
// (size="sm", no count). Variant carries the section meaning so the
// surrounding UI doesn't have to wrap chips in labeled containers.

export type ChipVariant = "skill" | "review" | "tag";
export type ChipSize = "sm" | "md";

const STYLES: Record<
    ChipVariant,
    {
        idle: string;
        active: string;
        countIdle: string;
        countActive: string;
        title: string;
    }
> = {
    skill: {
        idle: "bg-indigo-50 text-indigo-700 border-indigo-200 hover:bg-indigo-100 font-medium",
        active: "bg-indigo-600 text-white border-indigo-600 font-medium",
        countIdle: "text-indigo-400",
        countActive: "text-indigo-100",
        title: "skill",
    },
    review: {
        idle: "bg-rose-50 text-rose-700 border-rose-200 hover:bg-rose-100",
        active: "bg-rose-600 text-white border-rose-600",
        countIdle: "text-rose-400",
        countActive: "text-white/80",
        title: "review tag",
    },
    tag: {
        idle: "bg-gray-50 text-gray-500 border-gray-200 hover:bg-gray-100",
        active: "bg-studio-blue/10 text-studio-blue border-studio-blue/30",
        countIdle: "text-gray-400",
        countActive: "text-studio-blue/70",
        title: "task tag",
    },
};

// md gray-tag active uses solid studio-blue (the filter-rail chip variant);
// sm gray-tag active uses the softer 10% tint baked into STYLES.
const TAG_MD_ACTIVE = "bg-studio-blue text-white border-studio-blue";

const SIZE_CLS: Record<ChipSize, string> = {
    sm: "text-[10px] leading-none px-1.5 py-0.5",
    md: "text-xs px-2 py-0.5",
};

export function ChipButton({
    tag,
    count,
    variant,
    active,
    size,
    onClick,
    title,
}: {
    tag: string;
    count?: number;
    variant: ChipVariant;
    active: boolean;
    size: ChipSize;
    onClick?: () => void;
    title?: string;
}) {
    const s = STYLES[variant];
    const activeCls =
        variant === "tag" && size === "md" ? TAG_MD_ACTIVE : s.active;
    // Strip hover utilities when there's no click handler — otherwise the
    // non-interactive <span> branch shows a hover affordance it can't honor.
    const idleCls = onClick ? s.idle : s.idle.replace(/\s?hover:\S+/g, "");
    const stateCls = active ? activeCls : idleCls;
    const baseCls = `${SIZE_CLS[size]} rounded border transition-colors ${stateCls}`;
    const tooltip = title ?? s.title;
    const inner = (
        <>
            {tag}
            {count != null && (
                <span
                    className={`ml-1 tabular-nums ${active ? s.countActive : s.countIdle}`}
                >
                    {count}
                </span>
            )}
        </>
    );
    return onClick ? (
        <button
            type="button"
            onClick={onClick}
            aria-pressed={active}
            title={tooltip}
            className={baseCls}
        >
            {inner}
        </button>
    ) : (
        <span title={tooltip} className={baseCls}>
            {inner}
        </span>
    );
}
