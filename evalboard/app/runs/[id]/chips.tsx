"use client";

// Shared chip primitive for the run-detail page. Used in both the top
// filter rail (size="md", with count badge) and per-task rows in the grid
// (size="sm", no count). Variant carries the section meaning so the
// surrounding UI doesn't have to wrap chips in labeled containers.

export type ChipVariant = "skill" | "review" | "tag";
export type ChipSize = "sm" | "md";

// The colour/size utilities behind these tokens live in app/globals.css under
// `@layer components`. A chip renders ~11k times on a nightly run page, so the
// composed utility string (~130 chars each) was the single largest contributor
// to that page's HTML — 1.4 MB of identical class text. Emitting short tokens
// instead moves that text into the stylesheet, where the browser parses it once
// and caches it across navigations.
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
        idle: "chip-skill",
        active: "chip-skill-on",
        countIdle: "text-indigo-400",
        countActive: "text-indigo-100",
        title: "skill",
    },
    review: {
        idle: "chip-review",
        active: "chip-review-on",
        countIdle: "text-rose-400",
        countActive: "text-white/80",
        title: "review tag",
    },
    tag: {
        idle: "chip-tag",
        active: "chip-tag-on",
        countIdle: "text-gray-400",
        countActive: "text-studio-blue/70",
        title: "task tag",
    },
};

// md gray-tag active uses solid studio-blue (the filter-rail chip variant);
// sm gray-tag active uses the softer 10% tint baked into STYLES.
const TAG_MD_ACTIVE = "chip-tag-on-md";

const SIZE_CLS: Record<ChipSize, string> = {
    sm: "chip-sm",
    md: "chip-md",
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
    // `chip-act` arms the hover rules, and only the interactive branch gets it —
    // otherwise the non-interactive <span> shows a hover affordance it can't
    // honor. (This replaces stripping `hover:` utilities out of the string.)
    const idleCls = onClick ? `${s.idle} chip-act` : s.idle;
    const stateCls = active ? activeCls : idleCls;
    const baseCls = `chip ${SIZE_CLS[size]} ${stateCls}`;
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
