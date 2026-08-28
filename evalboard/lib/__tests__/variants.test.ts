import { describe, expect, test } from "vitest";
import {
    DEFAULT_VARIANT_ID,
    isValidVariantId,
    taskVariantKey,
    variantsOf,
} from "../variants";
import { perTaskPassCounts } from "../status";

describe("isValidVariantId", () => {
    test("accepts the ids coder_eval writes", () => {
        expect(isValidVariantId(DEFAULT_VARIANT_ID)).toBe(true);
        expect(isValidVariantId("live-v1")).toBe(true);
        expect(isValidVariantId("preview_v2")).toBe(true);
        expect(isValidVariantId("sonnet-4.6")).toBe(true);
    });

    // The id is joined into a filesystem path AND a blob prefix, and run.json
    // rows are untyped, so every escape shape has to be rejected before either
    // side effect. Mirrors reports_junit._is_safe_component.
    test("rejects anything that could escape the run directory", () => {
        expect(isValidVariantId("")).toBe(false);
        expect(isValidVariantId(".")).toBe(false);
        expect(isValidVariantId("..")).toBe(false);
        expect(isValidVariantId("../etc/passwd")).toBe(false);
        expect(isValidVariantId("a/b")).toBe(false);
        expect(isValidVariantId("a\\b")).toBe(false);
        expect(isValidVariantId("/absolute")).toBe(false);
        expect(isValidVariantId("C:\\windows")).toBe(false);
    });

    test("rejects non-strings and over-long ids", () => {
        expect(isValidVariantId(null)).toBe(false);
        expect(isValidVariantId(undefined)).toBe(false);
        expect(isValidVariantId(42)).toBe(false);
        expect(isValidVariantId("a".repeat(128))).toBe(false);
    });

    // Unlike a task id, an internal slash is never legitimate: dataset expansion
    // nests the TASK id ("<suite>/<row>"), never the variant.
    test("is stricter than the task-id rule about slashes", () => {
        expect(isValidVariantId("suite/row")).toBe(false);
    });
});

describe("taskVariantKey", () => {
    test("a row with no variant reads as the default arm", () => {
        expect(taskVariantKey({ taskId: "t" })).toBe(taskVariantKey({
            taskId: "t",
            variantId: DEFAULT_VARIANT_ID,
        }));
        expect(taskVariantKey({ taskId: "t", variantId: null })).toBe(
            taskVariantKey({ taskId: "t", variantId: DEFAULT_VARIANT_ID }),
        );
    });

    test("two arms of one task get distinct keys", () => {
        expect(taskVariantKey({ taskId: "t", variantId: "a" })).not.toBe(
            taskVariantKey({ taskId: "t", variantId: "b" }),
        );
    });

    // The separator must not be forgeable from either side, or an arm could be
    // made to collide with a different (arm, task) pair.
    test("cannot be forged by an id containing the separator", () => {
        expect(taskVariantKey({ taskId: "b c", variantId: "a" })).not.toBe(
            taskVariantKey({ taskId: "c", variantId: "a b" }),
        );
    });
});

describe("variantsOf", () => {
    test("an ordinary run reports a single arm", () => {
        expect(variantsOf([{ variantId: null }, {}])).toEqual([
            DEFAULT_VARIANT_ID,
        ]);
    });

    test("arms come back sorted and deduplicated", () => {
        expect(
            variantsOf([
                { variantId: "preview-v2" },
                { variantId: "live-v1" },
                { variantId: "preview-v2" },
            ]),
        ).toEqual(["live-v1", "preview-v2"]);
    });
});

describe("perTaskPassCounts under variants", () => {
    // The regression gate: a run with no variants must roll up exactly as it did
    // when the key was the task id alone.
    test("single-arm run rolls up one entry per task", () => {
        const counts = perTaskPassCounts([
            { taskId: "t1", status: "SUCCESS" },
            { taskId: "t1", status: "FAILURE" },
            { taskId: "t2", status: "SUCCESS" },
        ]);
        expect(counts.size).toBe(2);
        expect(counts.get(taskVariantKey({ taskId: "t1" }))).toBe(1);
        expect(counts.get(taskVariantKey({ taskId: "t2" }))).toBe(1);
    });

    // The point of the change: one arm passing must not make the other arm's
    // failure disappear from the rollup.
    test("arms are counted separately, so a pass cannot mask a failure", () => {
        const counts = perTaskPassCounts([
            { taskId: "t1", variantId: "live-v1", status: "SUCCESS" },
            { taskId: "t1", variantId: "preview-v2", status: "FAILURE" },
        ]);
        expect(counts.size).toBe(2);
        expect(
            counts.get(taskVariantKey({ taskId: "t1", variantId: "live-v1" })),
        ).toBe(1);
        expect(
            counts.get(
                taskVariantKey({ taskId: "t1", variantId: "preview-v2" }),
            ),
        ).toBe(0);
    });

    test("replicates still collapse within one arm", () => {
        const counts = perTaskPassCounts([
            { taskId: "t1", variantId: "a", status: "SUCCESS" },
            { taskId: "t1", variantId: "a", status: "SUCCESS" },
            { taskId: "t1", variantId: "b", status: "FAILURE" },
        ]);
        expect(counts.get(taskVariantKey({ taskId: "t1", variantId: "a" }))).toBe(
            2,
        );
        expect(counts.get(taskVariantKey({ taskId: "t1", variantId: "b" }))).toBe(
            0,
        );
    });
});
