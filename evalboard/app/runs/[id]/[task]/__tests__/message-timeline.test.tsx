import { describe, expect, test } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { MessageEvent } from "@/lib/runs";
import { MessageTimelineSection } from "../_sections";

function makeMessage(overrides: Partial<MessageEvent> = {}): MessageEvent {
    return {
        index: 1,
        role: "assistant",
        startedAt: null,
        completedAt: null,
        generationMs: 1000,
        thinkingMs: null,
        textMs: 1000,
        toolGenMs: null,
        blockTypes: ["text"],
        thinkingText: null,
        text: "hello",
        toolUses: [],
        inputTokens: 5,
        outputTokens: 1200,
        cacheWriteTokens: 4_500,
        cacheReadTokens: 85_000,
        reasoningTokens: null,
        thinkingOutputTokens: null,
        textOutputTokens: 1200,
        model: null,
        costUsd: null,
        ...overrides,
    };
}

describe("MessageTimelineSection — table layout", () => {
    test("renders no section when no messages", () => {
        const { container } = render(<MessageTimelineSection messages={[]} />);
        expect(container.firstChild).toBeNull();
    });

    test("renders a single header row with the eight columns", () => {
        render(<MessageTimelineSection messages={[makeMessage()]} />);
        // Column labels are case-sensitive matches against the rendered header.
        expect(screen.getByText("#")).toBeInTheDocument();
        expect(screen.getByText("Gen")).toBeInTheDocument();
        expect(screen.getByText("Exec")).toBeInTheDocument();
        expect(screen.getByText("Content")).toBeInTheDocument();
        expect(screen.getByText("Out")).toBeInTheDocument();
        expect(screen.getByText("Cache W")).toBeInTheDocument();
        expect(screen.getByText("Cache R")).toBeInTheDocument();
        expect(screen.getByText("Cost")).toBeInTheDocument();
        // …and only once each — adding more rows should not duplicate headers.
        render(
            <MessageTimelineSection
                messages={[makeMessage({ index: 1 }), makeMessage({ index: 2 })]}
            />,
        );
        expect(screen.getAllByText("Cache W")).toHaveLength(2); // one per rendered section
    });

    test("each message row shows formatted per-message tokens", () => {
        // 1,200 → "1.2k", 4,500 → "4.5k", 85,000 → "85k", index "1" present.
        render(<MessageTimelineSection messages={[makeMessage()]} />);
        expect(screen.getByText("1.2k")).toBeInTheDocument();
        expect(screen.getByText("4.5k")).toBeInTheDocument();
        expect(screen.getByText("85k")).toBeInTheDocument();
    });

    test("missing per-message tokens render as em-dash, not zero", () => {
        const m = makeMessage({
            outputTokens: null,
            cacheWriteTokens: null,
            cacheReadTokens: null,
            costUsd: null,
        });
        render(<MessageTimelineSection messages={[m]} />);
        // Four em-dashes for the three blank token cells plus the cost cell.
        expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(4);
    });

    test("USD toggle prices the token columns and shows an 'estimated' badge", () => {
        // output 2000 · 15/MTok = 0.03 on claude-sonnet-4-6.
        const m = makeMessage({
            model: "claude-sonnet-4-6",
            outputTokens: 2000,
            cacheWriteTokens: null,
            cacheReadTokens: null,
        });
        render(<MessageTimelineSection messages={[m]} />);
        // Default: token count shown (fmtTokens(2000) = "2.0k"), no badge.
        expect(screen.getByText("2.0k")).toBeInTheDocument();
        expect(screen.queryByText(/estimated/i)).toBeNull();
        // Toggle to USD: the Out column shows the priced value + estimated badge.
        fireEvent.click(screen.getByRole("button", { name: "USD" }));
        expect(screen.getByText("$0.0300")).toBeInTheDocument();
        expect(screen.getByText(/estimated/i)).toBeInTheDocument();
        expect(screen.queryByText("2.0k")).toBeNull();
    });

    test("per-message cost renders as USD when priced", () => {
        // costUsd is computed upstream (lib/pricing.ts); the row just formats it.
        const m = makeMessage({ costUsd: 0.0123 });
        render(<MessageTimelineSection messages={[m]} />);
        expect(screen.getByText("$0.0123")).toBeInTheDocument();
    });

    test("Cost header has an ⓘ help bubble explaining per-message cost", () => {
        render(<MessageTimelineSection messages={[makeMessage()]} />);
        const trigger = screen.getByRole("button", {
            name: /What is Per-message cost/i,
        });
        expect(screen.queryByRole("tooltip")).toBeNull();
        fireEvent.click(trigger);
        const card = screen.getByRole("tooltip");
        expect(card).toHaveTextContent("Per-message cost");
        expect(card).toHaveTextContent("authoritative SDK number");
    });

    test("unpriced message shows an em-dash for cost, not $0.00", () => {
        const m = makeMessage({ costUsd: null });
        const { container } = render(
            <MessageTimelineSection messages={[m]} />,
        );
        // No "$" anywhere — the cost cell falls back to em-dash.
        expect(container.textContent).not.toContain("$");
    });

    test("renders one row per message (index column)", () => {
        const msgs = [
            makeMessage({ index: 1 }),
            makeMessage({ index: 2 }),
            makeMessage({ index: 3 }),
        ];
        const { container } = render(<MessageTimelineSection messages={msgs} />);
        const ol = within(container.querySelector("ol") as HTMLElement);
        // One <li> per message, regardless of how the rollup strip counts.
        expect(ol.getByText("1")).toBeInTheDocument();
        expect(ol.getByText("2")).toBeInTheDocument();
        expect(ol.getByText("3")).toBeInTheDocument();
    });
});

describe("MessageTimelineSection — expanded sub-rows", () => {
    test("thinking sub-row shows the thinking emission's real output tokens", () => {
        const m = makeMessage({
            blockTypes: ["thinking", "text"],
            thinkingText: "deep thoughts",
            thinkingMs: 4000,
            textMs: 1000,
            generationMs: 5000,
            outputTokens: 350,
            // Real per-emission output for the thinking block (reasoning_tokens
            // is ~always 0 and is no longer used for this).
            thinkingOutputTokens: 250,
            // text share = 1000/(1000+0) of (350-250) = 100
            textOutputTokens: 100,
        });
        const { container } = render(<MessageTimelineSection messages={[m]} />);
        // Scope to the <ol> so the rollup strip's "thinking" / "text" labels
        // don't collide with the row's kind chips. The <details> renders all
        // children eagerly in jsdom, so no toggle is needed.
        const ol = within(container.querySelector("ol") as HTMLElement);
        const thinkingChip = ol.getByText("thinking");
        const subGrid = thinkingChip.closest("div.grid") as HTMLElement;
        expect(within(subGrid).getByText("250")).toBeInTheDocument();
        // No "~" marker on thinking — taken from the recorded per-emission value.
        expect(within(subGrid).queryByText("~")).toBeNull();
    });

    test("text sub-row shows its recorded output tokens (no approx marker)", () => {
        const m = makeMessage({
            blockTypes: ["text"],
            // Long enough that the inline preview cap (100) clips, so
            // hasBody is true and the sub-row renders.
            text: "x".repeat(150),
            textMs: 1000,
            outputTokens: 100,
            textOutputTokens: 100,
        });
        const { container } = render(<MessageTimelineSection messages={[m]} />);
        const ol = within(container.querySelector("ol") as HTMLElement);
        const textChip = ol.getByText("text");
        const subGrid = textChip.closest("div.grid") as HTMLElement;
        // Per-emission value, shown exact — no "~" approximation marker anymore.
        expect(within(subGrid).getByText("100")).toBeInTheDocument();
        expect(within(subGrid).queryByText("~")).toBeNull();
    });

    test("sub-row leaves cache write and cache read cells empty", () => {
        const m = makeMessage({
            blockTypes: ["text"],
            text: "x".repeat(150),
            outputTokens: 100,
            cacheWriteTokens: 4_500,
            cacheReadTokens: 85_000,
            textOutputTokens: 100,
        });
        const { container } = render(<MessageTimelineSection messages={[m]} />);
        const ol = within(container.querySelector("ol") as HTMLElement);
        const textChip = ol.getByText("text");
        const subGrid = textChip.closest("div.grid") as HTMLElement;
        // CacheW / CacheR sub-row cells are aria-hidden placeholders. The
        // per-message values must not leak into the sub-row.
        expect(within(subGrid).queryByText("4.5k")).toBeNull();
        expect(within(subGrid).queryByText("85k")).toBeNull();
    });

    test("tool sub-row shows execution time alongside generation time", () => {
        const m = makeMessage({
            blockTypes: ["tool_use"],
            text: null,
            textMs: null,
            toolGenMs: 500,
            generationMs: 500,
            toolUses: [
                {
                    toolName: "Bash",
                    toolUseId: "tu_1",
                    summary: "ls",
                    argText: "ls",
                    description: null,
                    genMs: 500,
                    durationMs: 1234,
                    isError: false,
                    resultPreview: null,
                    outputTokens: 80,
                },
            ],
            outputTokens: 80,
            reasoningTokens: 0,
            textOutputTokens: null,
        });
        const { container } = render(<MessageTimelineSection messages={[m]} />);
        container.querySelector("details")!.setAttribute("open", "");
        // "1.2s" is the formatted exec time for 1234ms in the sub-row's Exec
        // column — verifies that durationMs propagates onto the same grid.
        expect(screen.getAllByText("1.2s").length).toBeGreaterThanOrEqual(1);
    });
});
