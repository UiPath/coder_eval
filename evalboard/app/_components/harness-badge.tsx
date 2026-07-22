import Image from "next/image";

// The harnesses (coder-eval AgentKinds) the nightly rotates through, in display
// order. A run whose RunConfig carries no harness is a legacy claude-code run
// (see normalizeHarness in lib/overview.ts), so claude-code leads the list and
// is the default everywhere a harness must be assumed.
export const KNOWN_HARNESSES = ["claude-code", "codex", "antigravity"] as const;
export type KnownHarness = (typeof KNOWN_HARNESSES)[number];

// Vendor logo + labels for a run's harness (RunConfig). Renders the recognizable
// vendor mark instead of raw "claude-code"/"codex"/"antigravity" text,
// mirroring the Slack rollup's vendor emoji. A missing harness defaults to
// claude-code (the nightly). This is an internal-only column — the caller
// gates it behind isInternal (see lib/edition.ts).
const HARNESS_LOGO: Record<string, { src: string; label: string; short: string }> = {
    "claude-code": {
        src: "/harness/claude-code.png",
        label: "Claude Code · Anthropic",
        short: "Claude Code",
    },
    codex: { src: "/harness/codex.png", label: "Codex · OpenAI", short: "Codex" },
    antigravity: {
        src: "/harness/antigravity.png",
        label: "Antigravity · Google Gemini",
        short: "Antigravity",
    },
};

// Short human label for a harness id ("Claude Code"), for selectors and prose.
// Unknown ids fall through to the raw id.
export function harnessShortLabel(harness: string): string {
    return HARNESS_LOGO[harness]?.short ?? harness;
}

export function HarnessBadge({ harness }: { harness?: string | null }) {
    const key = harness ?? "claude-code";
    const logo = HARNESS_LOGO[key];
    // Unknown harness: show the raw id rather than a misleading logo.
    if (!logo) {
        return <span className="text-xs text-gray-700">{key}</span>;
    }
    return (
        <Image
            src={logo.src}
            alt={logo.label}
            title={logo.label}
            width={20}
            height={20}
            className="rounded-sm"
        />
    );
}
