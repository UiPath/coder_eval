import { describe, expect, test, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";

// vi.hoisted ensures these are initialized before vi.mock hoists its factory.
const { mockReplace, navState } = vi.hoisted(() => ({
    mockReplace: vi.fn(),
    navState: { q: "" as string },
}));

vi.mock("next/navigation", () => ({
    useRouter: () => ({ replace: mockReplace }),
    usePathname: () => "/",
    useSearchParams: () => new URLSearchParams(navState.q ? `q=${navState.q}` : ""),
}));

const { SearchBox } = await import("../search-box");

describe("SearchBox — typing-ahead race condition", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        mockReplace.mockClear();
        navState.q = "";
    });
    afterEach(() => {
        vi.useRealTimers();
    });

    test("preserves in-progress input when a navigation resolves mid-typing", () => {
        // Reproduces the race: user types "foo" → debounce fires → user types
        // more → navigation for "foo" resolves → input must NOT reset to "foo".
        const { rerender } = render(<SearchBox />);
        const input = screen.getByRole("textbox");

        fireEvent.change(input, { target: { value: "foo" } });

        // Debounce fires; typingAhead becomes false.
        act(() => { vi.advanceTimersByTime(300); });
        expect(mockReplace).toHaveBeenCalledOnce();

        // User types more before the navigation resolves.
        fireEvent.change(input, { target: { value: "foobar" } });

        // Navigation for "foo" resolves — URL now reports "foo".
        navState.q = "foo";
        rerender(<SearchBox />);

        // typingAhead is true, so the sync effect must NOT overwrite the input.
        expect(input).toHaveValue("foobar");
    });

    test("syncs from URL when the user is not typing (external navigation)", () => {
        // Back/forward nav or a tag click should still update the input when the
        // user hasn't typed anything since the last URL write.
        const { rerender } = render(<SearchBox />);
        const input = screen.getByRole("textbox");

        navState.q = "tag:alpha";
        rerender(<SearchBox />);

        expect(input).toHaveValue("tag:alpha");
    });

    test("clears the input when the URL is cleared externally", () => {
        navState.q = "foo";
        const { rerender } = render(<SearchBox />);
        const input = screen.getByRole("textbox");

        expect(input).toHaveValue("foo");

        navState.q = "";
        rerender(<SearchBox />);

        expect(input).toHaveValue("");
    });
});
