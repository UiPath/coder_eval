import { describe, expect, test } from "vitest";
import { parseConversation } from "../runs";

describe("parseConversation", () => {
    test("parses a two-turn dialog", () => {
        const raw = [
            "=== USER (turn 1) ===",
            "please write fizzbuzz",
            "",
            "=== AGENT (turn 1) ===",
            "here is the code",
            "",
        ].join("\n");

        const turns = parseConversation(raw);

        expect(turns).toHaveLength(2);
        expect(turns[0]).toEqual({
            role: "USER",
            turn: 1,
            metadata: null,
            text: "please write fizzbuzz",
        });
        expect(turns[1].role).toBe("AGENT");
        expect(turns[1].text).toBe("here is the code");
    });

    test("returns [] for empty input (the non-simulation gate)", () => {
        expect(parseConversation("")).toEqual([]);
    });

    test("captures metadata after the em-dash", () => {
        const raw = "=== USER (turn 2) — stop_token ===\ndone";
        expect(parseConversation(raw)[0].metadata).toBe("stop_token");
    });

    test("keeps multi-line bodies intact", () => {
        const raw = ["=== AGENT (turn 1) ===", "line one", "line two"].join(
            "\n",
        );
        expect(parseConversation(raw)[0].text).toBe("line one\nline two");
    });
});
