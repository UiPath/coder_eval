export function humanizeTaskId(id: string | null | undefined): string {
    if (!id) return "—";
    const stripped = id.replace(/^skill-flow-+/, "");
    const spaced = stripped.replace(/-+/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function fmtRunTime(id: string): string {
    const [d, t] = id.split("_");
    if (!d || !t) return id;
    return `${d} · ${t.replace(/-/g, ":")}`;
}
