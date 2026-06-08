// Collapsible list of component versions (cli / agent / sdk / drawer …). A run
// can stamp many of these and the flat wrap turned the run header into a wall of
// SHAs. Here the first few stay inline and the rest fold behind a native
// <details> disclosure — no client JS, so it works in the server-rendered run
// header.

export type ComponentSha = { name: string; sha: string; url: string | null };

// Mirror of lib/runs' SHA shape check (that module pulls in node:fs and can't be
// imported here). SHA-shaped values get truncated; human version strings don't.
const SHA_RE = /^[0-9a-f]{7,40}$/i;

// How many versions to show before the rest fold away.
const INLINE = 3;

export function VersionChip({ name, sha, url }: ComponentSha) {
    const isSha = SHA_RE.test(sha);
    const display = isSha && sha.length > 9 ? sha.slice(0, 9) : sha;
    return (
        <span className="inline-flex items-center gap-1 whitespace-nowrap">
            <span className="text-gray-400">{name}:</span>
            {url ? (
                <a
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-blue-600 hover:underline"
                >
                    {display}
                </a>
            ) : (
                <span className="text-gray-400">{display}</span>
            )}
        </span>
    );
}

export function VersionList({ versions }: { versions: ComponentSha[] }) {
    if (versions.length === 0) return null;

    // Few enough to show flat — no disclosure needed.
    if (versions.length <= INLINE + 1) {
        return (
            <div className="text-xs text-gray-500 font-mono pt-1 flex flex-wrap gap-x-3 gap-y-1">
                {versions.map((c) => (
                    <VersionChip key={c.name} {...c} />
                ))}
            </div>
        );
    }

    const head = versions.slice(0, INLINE);
    const rest = versions.slice(INLINE);
    return (
        <details className="group text-xs text-gray-500 font-mono pt-1">
            <summary className="flex flex-wrap items-center gap-x-3 gap-y-1 cursor-pointer list-none [&::-webkit-details-marker]:hidden">
                {head.map((c) => (
                    <VersionChip key={c.name} {...c} />
                ))}
                <span className="inline-flex items-center gap-1 rounded border border-dashed border-gray-300 px-1.5 py-0.5 text-gray-500 hover:bg-gray-50 hover:text-gray-700 transition-colors">
                    <span
                        aria-hidden
                        className="inline-block transition-transform group-open:rotate-90"
                    >
                        ▸
                    </span>
                    <span className="group-open:hidden">
                        +{rest.length} more
                    </span>
                    <span className="hidden group-open:inline">show less</span>
                </span>
            </summary>
            <div className="flex flex-wrap gap-x-3 gap-y-1 pt-1">
                {rest.map((c) => (
                    <VersionChip key={c.name} {...c} />
                ))}
            </div>
        </details>
    );
}
