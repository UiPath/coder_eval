export async function mapWithConcurrency<T, U>(
    items: T[],
    limit: number,
    fn: (item: T) => Promise<U>,
): Promise<U[]> {
    const out: U[] = new Array(items.length);
    let cursor = 0;
    async function worker() {
        while (true) {
            const i = cursor++;
            if (i >= items.length) return;
            out[i] = await fn(items[i]);
        }
    }
    await Promise.all(
        Array.from({ length: Math.min(limit, items.length) }, worker),
    );
    return out;
}

export function createLimiter(
    limit: number,
): <T>(fn: () => Promise<T>) => Promise<T> {
    let active = 0;
    const waiting: (() => void)[] = [];
    return async (fn) => {
        if (active < limit) active++;
        else await new Promise<void>((resolve) => waiting.push(resolve));
        try {
            return await fn();
        } finally {
            const next = waiting.shift();
            if (next) next();
            else active--;
        }
    };
}
