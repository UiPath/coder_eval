import { afterEach, describe, expect, test, vi } from "vitest";

// edition.ts captures EVALBOARD_EDITION at module-load time, so each case must
// set the env var and re-import a fresh module instance (vi.resetModules()
// clears the registry; a dynamic import then re-evaluates the module).
async function loadEdition(value: string | undefined) {
    vi.resetModules();
    if (value === undefined) {
        delete process.env.EVALBOARD_EDITION;
    } else {
        process.env.EVALBOARD_EDITION = value;
    }
    return import("../edition");
}

describe("edition gate", () => {
    afterEach(() => {
        delete process.env.EVALBOARD_EDITION;
    });

    test('EVALBOARD_EDITION="internal" => internal edition', async () => {
        const { EDITION, isInternal } = await loadEdition("internal");
        expect(EDITION).toBe("internal");
        expect(isInternal).toBe(true);
    });

    test("unset => OSS (the fail-closed default)", async () => {
        const { EDITION, isInternal } = await loadEdition(undefined);
        expect(EDITION).toBe("oss");
        expect(isInternal).toBe(false);
    });

    // Exact, case-sensitive match on "internal" — everything else is OSS.
    // Guards against a future inversion (e.g. `!== "oss"` or defaulting to
    // internal) silently shipping internal-only surfaces into the OSS build.
    test.each(["", "oss", "OSS", "INTERNAL", "Internal", "internal ", "public"])(
        "%j => OSS",
        async (value) => {
            const { EDITION, isInternal } = await loadEdition(value);
            expect(EDITION).toBe("oss");
            expect(isInternal).toBe(false);
        },
    );
});
