import { describe, expect, test } from "vitest";
import { isValidId, isValidTaskId } from "../blob";

describe("isValidId", () => {
    test("accepts simple alphanumeric IDs", () => {
        expect(isValidId("abc")).toBe(true);
        expect(isValidId("run-2026-06-03")).toBe(true);
        expect(isValidId("task_v2.1")).toBe(true);
    });

    test("rejects empty string", () => {
        expect(isValidId("")).toBe(false);
    });

    test("rejects IDs with slashes", () => {
        expect(isValidId("sentiment-classification/r3")).toBe(false);
        expect(isValidId("a/b")).toBe(false);
    });

    test("rejects non-string values", () => {
        expect(isValidId(null)).toBe(false);
        expect(isValidId(undefined)).toBe(false);
        expect(isValidId(42)).toBe(false);
    });

    test("rejects path traversal attempts", () => {
        expect(isValidId("../etc/passwd")).toBe(false);
        expect(isValidId("foo/../bar")).toBe(false);
    });
});

describe("isValidTaskId", () => {
    test("accepts simple task IDs (no slash)", () => {
        expect(isValidTaskId("sentiment-classification")).toBe(true);
        expect(isValidTaskId("task_v2.1")).toBe(true);
    });

    test("accepts dataset-expanded task IDs with one slash separator", () => {
        expect(isValidTaskId("sentiment-classification/r3")).toBe(true);
        expect(isValidTaskId("my-task/r10")).toBe(true);
    });

    test("accepts task IDs with multiple slash-separated segments", () => {
        expect(isValidTaskId("suite/task/r1")).toBe(true);
    });

    test("rejects empty string", () => {
        expect(isValidTaskId("")).toBe(false);
    });

    test("rejects leading or trailing slashes", () => {
        expect(isValidTaskId("/sentiment-classification")).toBe(false);
        expect(isValidTaskId("sentiment-classification/")).toBe(false);
    });

    test("rejects consecutive slashes", () => {
        expect(isValidTaskId("a//b")).toBe(false);
    });

    test("rejects path traversal attempts", () => {
        expect(isValidTaskId("../etc/passwd")).toBe(false);
        expect(isValidTaskId("foo/../../etc")).toBe(false);
        expect(isValidTaskId("foo/../bar")).toBe(false);
    });

    test("rejects non-string values", () => {
        expect(isValidTaskId(null)).toBe(false);
        expect(isValidTaskId(undefined)).toBe(false);
    });
});
