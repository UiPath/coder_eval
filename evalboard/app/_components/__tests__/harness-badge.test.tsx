import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { KNOWN_HARNESSES } from "@/lib/harness";
import { HarnessBadge, harnessShortLabel } from "../harness-badge";

describe("harnessShortLabel", () => {
    test("every known harness has a human label", () => {
        // A missing entry falls through to the raw id, which would put
        // "delegate-sdk" in a legend next to "Codex" and "Antigravity".
        for (const h of KNOWN_HARNESSES) {
            expect(harnessShortLabel(h)).not.toBe(h);
        }
    });

    test("the UiPath harness reads as Delegate", () => {
        // The run data's id stays `delegate-sdk` (it's the registered
        // `agent.type`); only the label people read is the short one.
        expect(harnessShortLabel("delegate-sdk")).toBe("Delegate");
    });

    test("an unknown harness falls back to its id rather than a wrong name", () => {
        expect(harnessShortLabel("some-new-agent")).toBe("some-new-agent");
    });
});

describe("HarnessBadge", () => {
    test("names the vendor in the alt text, not just the product", () => {
        render(<HarnessBadge harness="delegate-sdk" />);
        expect(screen.getByAltText("Delegate · UiPath")).toBeInTheDocument();
    });

    test("renders the id as text when there is no logo for it", () => {
        // Better a raw id than another vendor's mark on someone else's run.
        render(<HarnessBadge harness="some-new-agent" />);
        expect(screen.getByText("some-new-agent")).toBeInTheDocument();
    });

    test("takes a size so the chart legend can sit inside 11px text", () => {
        render(<HarnessBadge harness="codex" size={14} />);
        expect(screen.getByAltText("Codex · OpenAI")).toHaveAttribute(
            "width",
            "14",
        );
    });
});
