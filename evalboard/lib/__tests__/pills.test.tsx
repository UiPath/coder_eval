import { describe, expect, test } from "vitest";
import { render } from "@testing-library/react";
import { StatusPill } from "../pills";

function pill(status: string | null, relabel = false) {
    const { container } = render(
        <StatusPill status={status} relabel={relabel} />,
    );
    return container.firstChild as HTMLElement;
}

describe("StatusPill — colour by outcome", () => {
    test("SUCCESS is green", () => {
        expect(pill("SUCCESS").className).toContain("text-green-700");
    });

    test.each([
        "FAILURE",
        "ERROR",
        "TIMEOUT",
        "MAX_TURNS_EXHAUSTED",
        "TOKEN_BUDGET_EXCEEDED",
        "COST_BUDGET_EXCEEDED",
    ])("%s is red (not grey)", (status) => {
        const el = pill(status);
        expect(el.className).toContain("text-red-700");
        expect(el.className).not.toContain("text-gray-600");
    });

    test("null renders a grey em-dash", () => {
        const el = pill(null);
        expect(el.className).toContain("text-gray-600");
        expect(el.textContent).toBe("—");
    });

    test("relabel keeps the specific status label but still colours red", () => {
        // MAX_TURNS_EXHAUSTED keeps its raw label (informative) while being red.
        const el = pill("MAX_TURNS_EXHAUSTED", true);
        expect(el.className).toContain("text-red-700");
        expect(el.textContent).toBe("MAX_TURNS_EXHAUSTED");
        // Generic FAILURE relabels to "Failed".
        expect(pill("FAILURE", true).textContent).toBe("Failed");
        expect(pill("SUCCESS", true).textContent).toBe("Passed");
    });
});
