import { describe, expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { KNOWN_HARNESSES, harnessColor } from "@/lib/harness";
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

// Recharts colors a line by harness; the selector segment that picks that line
// must carry the same swatch, or the page-level control and the chart below it
// read as two unrelated color schemes.
function swatchColors(container: HTMLElement): string[] {
    return [...container.querySelectorAll<HTMLElement>("span[style]")].map(
        (el) => el.style.backgroundColor,
    );
}

// jsdom normalizes an inline hex to rgb(); compare in that space.
function rgb(hex: string): string {
    const n = parseInt(hex.slice(1), 16);
    return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}

describe("HarnessSelector", () => {
    test("each segment wears its harness's chart-series color", () => {
        const { container } = render(
            <HarnessSelector
                current={null}
                harnesses={[...KNOWN_HARNESSES]}
                includeAll
            />,
        );
        expect(swatchColors(container)).toEqual(
            KNOWN_HARNESSES.map((h) => rgb(harnessColor(h))),
        );
    });

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
