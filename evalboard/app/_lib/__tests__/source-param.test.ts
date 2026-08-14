import { describe, expect, test } from "vitest";
import { SCRIBE_SOURCE, SKILLS_SOURCE } from "@/lib/sources";
import { scalarParam, withSource } from "../source-param";

describe("scalarParam", () => {
    test("passes a scalar through and unwraps a repeated param", () => {
        expect(scalarParam("scribe")).toBe("scribe");
        expect(scalarParam(["scribe", "skills"])).toBe("scribe");
    });

    test("absent stays absent (sourceById then picks the default)", () => {
        expect(scalarParam(undefined)).toBeUndefined();
        expect(scalarParam([])).toBeUndefined();
    });
});

describe("withSource", () => {
    test("omits the param for the default source so old URLs are unchanged", () => {
        expect(withSource("/runs/r1", SKILLS_SOURCE.id)).toBe("/runs/r1");
        expect(withSource("/runs/r1", undefined)).toBe("/runs/r1");
        expect(withSource("/runs/r1?r=2", SKILLS_SOURCE.id)).toBe("/runs/r1?r=2");
    });

    test("appends for a non-default source, respecting an existing query", () => {
        expect(withSource("/runs/r1", SCRIBE_SOURCE.id)).toBe(
            "/runs/r1?src=scribe",
        );
        expect(withSource("/runs/r1/t?r=2", SCRIBE_SOURCE.id)).toBe(
            "/runs/r1/t?r=2&src=scribe",
        );
    });
});
