import Image from "next/image";

// Canonical harness constants live in the leaf lib/harness module (shared by
// the server data layer); re-exported here so existing badge importers are
// unaffected.
export { KNOWN_HARNESSES, type KnownHarness } from "@/lib/harness";

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
