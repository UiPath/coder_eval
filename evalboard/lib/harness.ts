// Canonical harness (coder-eval AgentKind) constants. Leaf module with no
// server- or client-only dependencies, so both the client badge/selector and
// the server data layer can import it without pulling node-only code into the
// client bundle or creating an import cycle.

// The harnesses the nightly rotates through, in preferred display order. This
// is only a display-order hint / default source — the switcher is data-driven
// (see listRecentHarnesses), so a new harness like "delegate" surfaces
// automatically without editing this list.
export const KNOWN_HARNESSES = ["claude-code", "codex", "antigravity"] as const;
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
