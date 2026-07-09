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
