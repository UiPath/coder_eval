// Stays under the 300 s fresh-run TTL of the per-run cache in lib/overview.ts.
const DEFAULT_INTERVAL_SECONDS = 240;
const FIRST_WARM_DELAY_MS = 15_000;
const WARM_TIMEOUT_MS = 120_000;
const WARM_PATHS = ["/", "/series"];

type Env = Record<string, string | undefined>;

export function warmIntervalMs(env: Env): number | null {
    if (env.NODE_ENV !== "production") return null;
    const raw = env.EVALBOARD_WARM_INTERVAL_SECONDS;
    const seconds =
        raw === undefined || raw.trim() === ""
            ? DEFAULT_INTERVAL_SECONDS
            : Number(raw);
    return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : null;
}

export function warmUrl(env: Env): string {
    const host =
        env.HOSTNAME && env.HOSTNAME !== "0.0.0.0" ? env.HOSTNAME : "127.0.0.1";
    const port = Number.parseInt(env.PORT ?? "", 10) || 3000;
    return `http://${host}:${port}/`;
}

export function startFrontPageWarmer(
    env: Env = process.env,
    fetchImpl: typeof fetch = fetch,
): (() => void) | null {
    const interval = warmIntervalMs(env);
    if (interval === null) return null;
    const base = warmUrl(env);
    let inFlight = false;
    const warmOne = async (path: string) => {
        const url = new URL(path, base).toString();
        try {
            const res = await fetchImpl(url, {
                cache: "no-store",
                signal: AbortSignal.timeout(WARM_TIMEOUT_MS),
            });
            await res.arrayBuffer();
            if (!res.ok) console.warn(`evalboard warm: ${url} answered ${res.status}`);
        } catch (err) {
            console.warn(`evalboard warm: ${url} failed: ${String(err)}`);
        }
    };
    const warm = async () => {
        if (inFlight) return;
        inFlight = true;
        try {
            for (const path of WARM_PATHS) await warmOne(path);
        } finally {
            inFlight = false;
        }
    };
    let every: ReturnType<typeof setInterval> | undefined;
    const first = setTimeout(() => {
        void warm();
        every = setInterval(warm, interval);
        every.unref?.();
    }, FIRST_WARM_DELAY_MS);
    first.unref?.();
    return () => {
        clearTimeout(first);
        if (every !== undefined) clearInterval(every);
    };
}
