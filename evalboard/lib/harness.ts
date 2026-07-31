// Canonical harness (coder-eval AgentKind) constants. Leaf module with no
// server- or client-only dependencies, so both the client badge/selector and
// the server data layer can import it without pulling node-only code into the
// client bundle or creating an import cycle.

// The harnesses the nightly rotates through, in preferred display order. This
// is only a display-order hint / default source — the switcher is data-driven
// (see listRecentHarnesses), so a new harness surfaces automatically without
// editing this list. Membership here does buy two things: a stable position in
// the switcher, and a reserved series color (see HARNESS_COLORS).
export const KNOWN_HARNESSES = [
    "claude-code",
    "codex",
    "antigravity",
    "delegate-sdk",
] as const;
export type KnownHarness = (typeof KNOWN_HARNESSES)[number];

// A run with no RunConfig harness predates the stamp; every such run was
// claude-code (the only nightly harness before codex/antigravity), so
// null/undefined folds to the default. Mirrors HarnessBadge's default.
export const DEFAULT_HARNESS = "claude-code";
export function normalizeHarness(harness: string | null | undefined): string {
    return harness ?? DEFAULT_HARNESS;
}

// Accept any syntactically-plausible harness id, defaulting to claude-code when
// absent or malformed. Deliberately NOT whitelisted against KNOWN_HARNESSES so
// a newly-added harness (e.g. "delegate") is selectable the moment its runs
// exist — the value is only ever compared for equality against a run's stamped
// harness, never used in a path or query, so a bounded charset is enough.
export function parseHarnessParam(raw: string | string[] | undefined): string {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (typeof v !== "string") return DEFAULT_HARNESS;
    const trimmed = v.trim();
    return /^[\w.-]{1,64}$/.test(trimmed) ? trimmed : DEFAULT_HARNESS;
}

// URL value for the unscoped view. Reserved: a real harness id could never
// collide with it, since AgentKind ids are hyphenated vendor names.
export const ALL_HARNESSES = "all";

// Harness scope for pages that can show every harness at once (the overview).
// null = all harnesses, which is also the DEFAULT — the front page opens on the
// comparison view and narrows from there, so `?h=` absent means "everything"
// rather than "claude-code". Pages that genuinely need a single harness (trends
// collapses per-task history across runs, which is only meaningful within one
// harness) keep using parseHarnessParam.
export function parseHarnessScope(
    raw: string | string[] | undefined,
): string | null {
    const v = Array.isArray(raw) ? raw[0] : raw;
    if (typeof v !== "string") return null;
    const trimmed = v.trim();
    if (!trimmed || trimmed === ALL_HARNESSES) return null;
    return /^[\w.-]{1,64}$/.test(trimmed) ? trimmed : null;
}

// Series color per harness, for the multi-harness overview charts and their
// legends. Each is the vendor's own mark, so a line and the logo beside it in
// the legend read as the same harness. Color is bound to the HARNESS, not to
// its position in the series list — a filter that drops one harness must not
// repaint the others.
//
// Validated as a categorical set against a white chart surface over ALL pairs,
// not just adjacent ones: worst normal-vision ΔE 22.8, worst CVD ΔE 16.6
// (protan), every entry clears 3:1 contrast. Two deliberate departures from a
// synthetic palette, both forced by using real vendor marks:
//
//   - codex is OpenAI's near-black, which fails the lightness-band and
//     chroma-floor checks the way every gray does. It stays: it has the highest
//     contrast of the four, and no surface identifies it by color alone (chart
//     legend and tooltip both pair the color with the vendor logo and the
//     harness name).
//   - delegate-sdk is NOT UiPath orange (#FA4616), even though that is the
//     brand color. Against Anthropic's coral it scores ΔE 9.5 normal-vision /
//     5.1 deutan — below the hard floor, i.e. two lines a full-color reader
//     cannot tell apart. It takes the nearest deep red that separates instead.
//
// Re-run the palette validator before adding a fifth entry rather than
// eyeballing a new hue.
const HARNESS_COLORS: Record<string, string> = {
    "claude-code": "#d97757", // Anthropic coral
    codex: "#171717", // OpenAI near-black
    antigravity: "#4285f4", // Google blue
    "delegate-sdk": "#9b1c1c", // deep red (UiPath orange collides with Anthropic)
};

// Neutral for any harness with no reserved slot. Deliberately NOT a generated
// hue: unknown harnesses fold into one "other" color, which stays honest under
// CVD, and the legend still names each line.
const HARNESS_COLOR_FALLBACK = "#6b7280";

export function harnessColor(harness: string): string {
    return HARNESS_COLORS[harness] ?? HARNESS_COLOR_FALLBACK;
}

// Stable series order for charts and legends: known harnesses in display order,
// then any newcomers alphabetically. Same rule as listRecentHarnesses, applied
// to whichever subset actually has data in the current window.
export function orderHarnesses(harnesses: Iterable<string>): string[] {
    const seen = new Set(harnesses);
    const known = KNOWN_HARNESSES.filter((h) => seen.has(h));
    const extras = [...seen]
        .filter((h) => !(KNOWN_HARNESSES as readonly string[]).includes(h))
        .sort();
    return [...known, ...extras];
}
