export function humanizeTaskId(id: string | null | undefined): string {
    if (!id) return "—";
    const stripped = id.replace(/^skill-flow-+/, "");
    const spaced = stripped.replace(/-+/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

// Daily-pipeline run id: YYYY-MM-DD_HH-MM-SS. Only these reformat into a
// readable timestamp; anything else (ad-hoc run names like
// `codex_skills_full_v2`) is returned verbatim rather than mangled into a
// bogus `codex · skills`.
const DAILY_RUN_ID_RE = /^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$/;

export function fmtRunTime(id: string): string {
    if (!DAILY_RUN_ID_RE.test(id)) return id;
    const [d, t] = id.split("_");
    return `${d} · ${t.replace(/-/g, ":")}`;
}

// Compact human-readable count: 1234 -> "1.2k", 1_500_000 -> "1.5M".
// Used by the task page token cell where dense numbers compete for space
// with five other stats in the same dl grid.
export function fmtCompact(n: number | null | undefined): string {
    if (n == null) return "—";
    const abs = Math.abs(n);
    if (abs >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
    if (abs >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(n);
}

// USD with enough precision to read sub-cent differences as the thinking
// slider moves — small task runs land in the $0.0xx range.
export function fmtUsd(n: number | null | undefined): string {
    if (n == null || !Number.isFinite(n)) return "—";
    const abs = Math.abs(n);
    const digits = abs >= 1 ? 2 : abs >= 0.1 ? 3 : 4;
    return `$${n.toFixed(digits)}`;
}

export function fmtDuration(s: number | null): string {
    if (s == null) return "—";
    // Round once on the total to avoid `1m 60s` from rounding the remainder.
    const total = Math.round(s);
    if (total < 60) return `${total}s`;
    const m = Math.floor(total / 60);
    const rem = total - m * 60;
    if (m < 60) return `${m}m ${rem}s`;
    const h = Math.floor(m / 60);
    const mRem = m - h * 60;
    return `${h}h ${mRem}m`;
}
