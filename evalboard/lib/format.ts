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
