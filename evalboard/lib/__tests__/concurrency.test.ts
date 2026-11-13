import { describe, expect, test } from "vitest";
import { createLimiter } from "@/lib/concurrency";

describe("createLimiter", () => {
    test("never runs more than the limit at once and runs every call", async () => {
        const limit = createLimiter(2);
        let running = 0;
        let peak = 0;
        const results = await Promise.all(
            Array.from({ length: 7 }, (_, i) =>
                limit(async () => {
                    running++;
                    peak = Math.max(peak, running);
                    await new Promise((r) => setTimeout(r, 1));
                    running--;
                    return i;
                }),
            ),
        );
        expect(peak).toBe(2);
        expect(results).toEqual([0, 1, 2, 3, 4, 5, 6]);
    });

    test("a rejected call frees its slot", async () => {
        const limit = createLimiter(1);
        await expect(limit(() => Promise.reject(new Error("boom")))).rejects.toThrow(
            "boom",
        );
        await expect(limit(async () => "next")).resolves.toBe("next");
    });
});
