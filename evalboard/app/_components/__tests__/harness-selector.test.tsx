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

    test("a known harness absent from the discovery window still shows", () => {
        // Delegate runs weekly, so it falls out of the 12-run discovery window
        // between firings. The segment must survive that, or the filter reads as
        // "delegate was removed" rather than "delegate hasn't run lately".
        render(
            <HarnessSelector
                current={null}
                harnesses={["claude-code"]}
                includeAll
            />,
        );
        for (const h of KNOWN_HARNESSES) {
            expect(
                screen.getByRole("button", { name: harnessShortLabel(h) }),
            ).toBeInTheDocument();
        }
    });

    test("a discovered newcomer is kept, and sorts after the known set", () => {
        render(
            <HarnessSelector
                current={null}
                harnesses={["zzz-new-harness"]}
                includeAll
            />,
        );
        const names = screen
            .getAllByRole("button")
            .map((b) => b.getAttribute("aria-label"));
        expect(names).toContain(harnessShortLabel("zzz-new-harness"));
        // Known harnesses hold display order; the newcomer lands last.
        expect(names[names.length - 1]).toBe(
            harnessShortLabel("zzz-new-harness"),
        );
    });

    test("no segment is duplicated when discovery and the known set overlap", () => {
        render(
            <HarnessSelector
                current="codex"
                harnesses={[...KNOWN_HARNESSES]}
                includeAll
            />,
        );
        const labels = screen
            .getAllByRole("button")
            .map((b) => b.getAttribute("aria-label"))
            .filter((n): n is string => n != null);
        expect(new Set(labels).size).toBe(labels.length);
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
