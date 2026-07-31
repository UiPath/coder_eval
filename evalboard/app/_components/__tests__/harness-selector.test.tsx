import { describe, expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { KNOWN_HARNESSES } from "@/lib/harness";
import { harnessShortLabel } from "../harness-badge";

// The selector reads router/params hooks; stub them so it renders in jsdom
// without a router provider.
const replace = vi.fn();
vi.mock("next/navigation", () => ({
    useRouter: () => ({ replace }),
    usePathname: () => "/",
    useSearchParams: () => new URLSearchParams(),
}));

const { HarnessSelector } = await import("../harness-selector");

describe("HarnessSelector", () => {
    test("the all-harness segment is present and selected by default", () => {
        render(
            <HarnessSelector
                current={null}
                harnesses={[...KNOWN_HARNESSES]}
                includeAll
            />,
        );
        expect(screen.getByRole("button", { name: "All" })).toHaveAttribute(
            "aria-pressed",
            "true",
        );
    });

    test("a scoped harness reads as pressed, and All does not", () => {
        render(
            <HarnessSelector
                current="codex"
                harnesses={[...KNOWN_HARNESSES]}
                includeAll
            />,
        );
        expect(screen.getByRole("button", { name: "Codex" })).toHaveAttribute(
            "aria-pressed",
            "true",
        );
        expect(screen.getByRole("button", { name: "All" })).toHaveAttribute(
            "aria-pressed",
            "false",
        );
    });

    test("a deep-linked harness that has aged out still shows as a segment", () => {
        // Otherwise `?h=delegate-sdk` after a quiet fortnight renders a control
        // with nothing selected, which reads as an unscoped page.
        render(
            <HarnessSelector
                current="delegate-sdk"
                harnesses={["claude-code", "codex"]}
                includeAll
            />,
        );
        expect(
            screen.getByRole("button", { name: "Delegate" }),
        ).toHaveAttribute("aria-pressed", "true");
    });

    test("every segment is named for screen readers, not color-only", () => {
        render(
            <HarnessSelector
                current={null}
                harnesses={[...KNOWN_HARNESSES]}
                includeAll
            />,
        );
        // The accessible name comes from aria-label, so it survives the
        // below-`sm` breakpoint that hides the visible text.
        for (const h of KNOWN_HARNESSES) {
            expect(
                screen.getByRole("button", { name: harnessShortLabel(h) }),
            ).toBeInTheDocument();
        }
    });
});
