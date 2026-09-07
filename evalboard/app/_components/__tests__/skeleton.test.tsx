import { describe, expect, test } from "vitest";
import { render } from "@testing-library/react";
import RootLoading from "@/app/loading";
import RunLoading from "@/app/runs/[id]/loading";
import TaskLoading from "@/app/runs/[id]/[...task]/loading";

const CASES = [
    ["root", RootLoading, "Loading page"],
    ["run", RunLoading, "Loading run"],
    ["task", TaskLoading, "Loading task"],
] as const;

// Every class on every rendered node, flattened into tokens.
function classTokens(root: HTMLElement): string[] {
    const out: string[] = [];
    for (const el of root.querySelectorAll<HTMLElement>("*")) {
        out.push(...el.className.split(/\s+/).filter(Boolean));
    }
    return out;
}

// Tailwind's numeric width scale is quarter-rem, so `w-24` is 96px. Arbitrary
// values carry their own unit.
function fixedWidthPx(token: string): number | null {
    const scale = /^(?:min-)?w-(\d+(?:\.\d+)?)$/.exec(token);
    if (scale) return Number(scale[1]) * 4;
    const arb = /^(?:min-)?w-\[(\d+(?:\.\d+)?)px\]$/.exec(token);
    if (arb) return Number(arb[1]);
    return null;
}

// The narrowest phone the rest of the app is built to survive. A skeleton that
// overflows it would introduce a horizontal scrollbar that disappears the
// moment the real content arrives.
const MIN_VIEWPORT_PX = 320;
// Room for the page gutters (`px-4` each side in the root layout).
const CONTENT_BUDGET_PX = MIN_VIEWPORT_PX - 32;

describe.each(CASES)("%s loading skeleton", (_name, Loading, label) => {
    test("announces itself to a screen reader", () => {
        const { getByRole } = render(<Loading />);
        const status = getByRole("status");
        expect(status).toHaveAttribute("aria-busy", "true");
        expect(status).toHaveAttribute("aria-label", label);
        // The blocks are decorative, so the label has to carry the meaning.
        expect(status.textContent).toContain(label);
    });

    test("the pulse is dropped under prefers-reduced-motion", () => {
        const { getByRole } = render(<Loading />);
        const cls = getByRole("status").className;
        expect(cls).toContain("animate-pulse");
        expect(cls).toContain("motion-reduce:animate-none");
    });

    test("no fixed width can overflow a 320px phone", () => {
        const { container } = render(<Loading />);
        const offenders = classTokens(container)
            .map((t) => [t, fixedWidthPx(t)] as const)
            .filter(([, px]) => px != null && px > CONTENT_BUDGET_PX);
        expect(offenders.map(([t]) => t)).toEqual([]);
    });

    test("every wide block is capped rather than left to run", () => {
        const { container } = render(<Loading />);
        // `w-full` on its own is fine (it fills the parent); paired with a
        // `max-w-*` it is also fine. What must not appear is a fixed width
        // wider than the phone, which the test above covers, or a horizontal
        // overflow container, which would scroll instead of wrap.
        expect(classTokens(container)).not.toContain("overflow-x-auto");
    });

    test("the skeleton is small enough to be free", () => {
        const { container } = render(<Loading />);
        // The pages these stand in for render thousands of nodes; the point of
        // the skeleton is to appear instantly, so it must stay tiny.
        expect(container.querySelectorAll("*").length).toBeLessThan(200);
    });
});

describe("run skeleton", () => {
    test("mirrors the grid's own md breakpoint instead of picking one layout", () => {
        // task-grid renders a table at md and up and a card list below it. If
        // the skeleton showed only one, a phone (or a desktop) would watch the
        // layout jump the moment the real content landed.
        const { container } = render(<RunLoading />);
        expect(container.querySelector(".hidden.md\\:block")).not.toBeNull();
        expect(container.querySelector(".md\\:hidden")).not.toBeNull();
    });

    test("the stat tiles match the real two-column phone grid", () => {
        const { container } = render(<RunLoading />);
        const grid = container.querySelector(".grid");
        expect(grid?.className).toContain("grid-cols-2");
        expect(grid?.className).toContain("md:grid-cols-5");
    });
});
