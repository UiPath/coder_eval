// @vitest-environment node
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { startFrontPageWarmer, warmIntervalMs, warmUrl } from "../warm";

const PROD = { NODE_ENV: "production" };

describe("warmIntervalMs", () => {
    test("is off outside production", () => {
        expect(warmIntervalMs({ NODE_ENV: "development" })).toBeNull();
        expect(warmIntervalMs({})).toBeNull();
    });

    test("defaults to four minutes in production", () => {
        expect(warmIntervalMs(PROD)).toBe(240_000);
        expect(warmIntervalMs({ ...PROD, EVALBOARD_WARM_INTERVAL_SECONDS: " " })).toBe(240_000);
    });

    test("takes an interval in seconds, and 0 or junk turns it off", () => {
        expect(warmIntervalMs({ ...PROD, EVALBOARD_WARM_INTERVAL_SECONDS: "60" })).toBe(60_000);
        expect(warmIntervalMs({ ...PROD, EVALBOARD_WARM_INTERVAL_SECONDS: "0" })).toBeNull();
        expect(warmIntervalMs({ ...PROD, EVALBOARD_WARM_INTERVAL_SECONDS: "-5" })).toBeNull();
        expect(warmIntervalMs({ ...PROD, EVALBOARD_WARM_INTERVAL_SECONDS: "soon" })).toBeNull();
    });
});

describe("warmUrl", () => {
    test("targets the address the standalone server binds", () => {
        expect(warmUrl({})).toBe("http://127.0.0.1:3000/");
        expect(warmUrl({ HOSTNAME: "0.0.0.0", PORT: "8080" })).toBe("http://127.0.0.1:8080/");
        expect(warmUrl({ HOSTNAME: "a1b2c3", PORT: "8080" })).toBe("http://a1b2c3:8080/");
        expect(warmUrl({ PORT: "not-a-port" })).toBe("http://127.0.0.1:3000/");
    });
});

describe("startFrontPageWarmer", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.spyOn(console, "warn").mockImplementation(() => {});
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    const ok = () =>
        vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response("<html/>", { status: 200 }));

    test("does nothing when warming is off", () => {
        const fetchImpl = ok();
        expect(startFrontPageWarmer({ NODE_ENV: "development" }, fetchImpl)).toBeNull();
        vi.advanceTimersByTime(600_000);
        expect(fetchImpl).not.toHaveBeenCalled();
    });

    test("warms shortly after start, then on every interval", async () => {
        const fetchImpl = ok();
        const stop = startFrontPageWarmer({ ...PROD, PORT: "8080" }, fetchImpl);
        await vi.advanceTimersByTimeAsync(14_999);
        expect(fetchImpl).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(1);
        expect(fetchImpl.mock.calls.map((c) => c[0])).toEqual([
            "http://127.0.0.1:8080/",
            "http://127.0.0.1:8080/series",
        ]);
        await vi.advanceTimersByTimeAsync(240_000);
        expect(fetchImpl).toHaveBeenCalledTimes(4);
        await vi.advanceTimersByTimeAsync(240_000);
        expect(fetchImpl).toHaveBeenCalledTimes(6);
        stop?.();
        await vi.advanceTimersByTimeAsync(600_000);
        expect(fetchImpl).toHaveBeenCalledTimes(6);
    });

    test("skips a round while the previous warm is still rendering", async () => {
        const fetchImpl = vi.fn(() => new Promise<Response>(() => {}));
        startFrontPageWarmer({ ...PROD, EVALBOARD_WARM_INTERVAL_SECONDS: "10" }, fetchImpl);
        await vi.advanceTimersByTimeAsync(60_000);
        expect(fetchImpl).toHaveBeenCalledTimes(1);
    });

    test("a failed warm warns and the next round still runs", async () => {
        const fetchImpl = vi
            .fn()
            .mockRejectedValueOnce(new Error("ECONNREFUSED"))
            .mockResolvedValue(new Response("", { status: 500 }));
        startFrontPageWarmer({ ...PROD, EVALBOARD_WARM_INTERVAL_SECONDS: "10" }, fetchImpl);
        await vi.advanceTimersByTimeAsync(15_000);
        await vi.advanceTimersByTimeAsync(10_000);
        expect(fetchImpl).toHaveBeenCalledTimes(4);
        expect(console.warn).toHaveBeenCalledWith(expect.stringContaining("ECONNREFUSED"));
        expect(console.warn).toHaveBeenCalledWith(expect.stringContaining("answered 500"));
    });

    test("a failure on one page does not stop the next from warming", async () => {
        const fetchImpl = vi
            .fn()
            .mockRejectedValueOnce(new Error("ECONNREFUSED"))
            .mockResolvedValue(new Response("", { status: 200 }));
        startFrontPageWarmer({ ...PROD, PORT: "8080" }, fetchImpl);
        await vi.advanceTimersByTimeAsync(15_000);
        expect(fetchImpl.mock.calls.map((c) => c[0])).toEqual([
            "http://127.0.0.1:8080/",
            "http://127.0.0.1:8080/series",
        ]);
    });
});
