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
        parentToolUseId: null,
        reasoningTokens: null,
        thinkingOutputTokens: null,
        textOutputTokens: 1200,
        model: null,
        costUsd: null,
        note: null,
        ...overrides,
    };
}

describe("MessageTimelineSection — table layout", () => {
    test("renders no section when no messages", () => {
        const { container } = render(<MessageTimelineSection messages={[]} />);
        expect(container.firstChild).toBeNull();
    });

    test("renders a single header row with the nine columns", () => {
        render(<MessageTimelineSection messages={[makeMessage()]} />);
        // Column labels are case-sensitive matches against the rendered header.
        expect(screen.getByText("#")).toBeInTheDocument();
        expect(screen.getByText("Gen")).toBeInTheDocument();
        expect(screen.getByText("Exec")).toBeInTheDocument();
        expect(screen.getByText("Content")).toBeInTheDocument();
        expect(screen.getByText("In")).toBeInTheDocument();
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

    test("Cost header explains per-message cost in its title, no ⓘ bubble", () => {
        render(<MessageTimelineSection messages={[makeMessage()]} />);
        expect(screen.getByText("Cost")).toHaveAttribute(
            "title",
            expect.stringContaining("authoritative SDK number"),
        );
        expect(
            screen.queryByRole("button", { name: /What is/i }),
        ).toBeNull();
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
                    resultTokens: null,
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

    test("renders blocks in emission order: text before a later tool call", () => {
        // blockTypes records true emission order. A generation that emits text
        // ("I'm writing 5050 now") THEN a tool call must render the text ABOVE
        // the tool row — not the old fixed thinking→tools→text layout.
        const m = makeMessage({
            blockTypes: ["text", "tool_use"],
            text: "WRITE_PREAMBLE_TEXT",
            toolUses: [
                {
                    toolName: "Bash",
                    toolUseId: "tu_1",
                    summary: "write",
                    argText: "WROTE_ANSWER_FILE",
                    description: null,
                    genMs: 10,
                    durationMs: 10,
                    isError: false,
                    resultPreview: null,
                    outputTokens: 5,
                    resultTokens: null,
                },
            ],
        });
        const { container } = render(<MessageTimelineSection messages={[m]} />);
        const body = container.textContent ?? "";
        // The collapsed summary repeats the tool arg, so compare against the
        // tool's BODY occurrence (lastIndexOf): the text must precede it.
        expect(body.indexOf("WRITE_PREAMBLE_TEXT")).toBeGreaterThanOrEqual(0);
        expect(body.indexOf("WRITE_PREAMBLE_TEXT")).toBeLessThan(body.lastIndexOf("WROTE_ANSWER_FILE"));
    });
});

describe("MessageTimelineSection — sub-agent grouping", () => {
    function agentTool(toolUseId: string) {
        return {
            toolName: "Agent",
            toolUseId,
            summary: "spawn sub-agent",
            argText: "sort left half",
            description: null,
            genMs: 100,
            durationMs: 2000,
            isError: false,
            resultPreview: "[1, 2]",
            outputTokens: 10,
            resultTokens: null,
        };
    }

    test("a sub-agent's emissions render nested under the Agent call that spawned them", () => {
        const parent = makeMessage({
            index: 1,
            text: "PARENT_THREAD",
            blockTypes: ["tool_use"],
            toolUses: [agentTool("T1")],
        });
        const child = makeMessage({
            index: 2,
            text: "CHILD_SUBAGENT",
            parentToolUseId: "T1", // ran inside the sub-agent spawned by T1
        });
        const { container } = render(
            <MessageTimelineSection messages={[parent, child]} />,
        );
        // The child is NOT a top-level row — it lives inside the spawning
        // Agent call's expansion. Open all disclosures to reveal it.
        const details = container.querySelectorAll("details");
        details.forEach((d) => d.setAttribute("open", ""));
        // The child's emission renders FLAT inside the Agent group (no
        // per-message disclosure of its own).
        expect(screen.getByText("CHILD_SUBAGENT")).toBeInTheDocument();
        expect(screen.getByText("PARENT_THREAD")).toBeInTheDocument();
        // The Agent call's result is rendered too (after the nested rows).
        expect(screen.getByText(/\[1, 2\]/)).toBeInTheDocument();
    });

    test("legacy run without branch info renders every message at top level", () => {
        // No parent_tool_use_id recorded → both messages are top-level rows, and
        // a message with an Agent tool call has no children to nest.
        const a = makeMessage({
            index: 1,
            text: "ROW_A",
            blockTypes: ["tool_use"],
            toolUses: [agentTool("T1")],
            parentToolUseId: undefined,
        });
        const b = makeMessage({
            index: 2,
            text: "ROW_B",
            parentToolUseId: undefined,
        });
        const { container } = render(
            <MessageTimelineSection messages={[a, b]} />,
        );
        // Two top-level rows in the table body.
        const topRows = container.querySelectorAll("ol > li");
        expect(topRows).toHaveLength(2);
        expect(screen.getByText("ROW_A")).toBeInTheDocument();
        expect(screen.getByText("ROW_B")).toBeInTheDocument();
    });

    test("shows each sub-agent call's token buckets on its per-call CallTokensRow", () => {
        const parent = makeMessage({
            index: 1,
            text: "MAIN",
            blockTypes: ["tool_use"],
            toolUses: [agentTool("T1")],
        });
        // The sub-agent's own (bubbled) call — its input-side buckets surface on
        // the per-call "call tokens" row inside the expanded Agent invocation,
        // NOT on the aggregate result row (which now only previews the return).
        const child = makeMessage({
            index: 2,
            parentToolUseId: "T1",
            blockTypes: ["tool_use"],
            inputTokens: 47,
            cacheReadTokens: 14349,
            cacheWriteTokens: 234,
            outputTokens: 121,
            toolUses: [
                {
                    toolName: "Bash",
                    toolUseId: "B1",
                    summary: "echo",
                    argText: "echo hi",
                    description: null,
                    genMs: 10,
                    durationMs: 20,
                    isError: false,
                    resultPreview: "hi",
                    outputTokens: 121,
                    resultTokens: null,
                },
            ],
        });
        const { container } = render(
            <MessageTimelineSection
                messages={[parent, child]}
                subAgentUsageByToolId={{
                    T1: {
                        total: 14751,
                        input: 47,
                        output: 121,
                        cacheCreation: 234,
                        cacheRead: 14349,
                    },
                }}
            />,
        );
        container
            .querySelectorAll("details")
            .forEach((d) => d.setAttribute("open", ""));
        expect(screen.queryByText(/sub-agent total:/)).not.toBeInTheDocument();
        // The "call tokens" row carries the call's input-side buckets:
        // cacheRead 14349 → "14k", cacheCreation 234 → "234".
        expect(screen.getAllByText("call tokens").length).toBeGreaterThan(0);
        expect(screen.getByText("14k")).toBeInTheDocument();
        expect(screen.getByText("234")).toBeInTheDocument();
    });

    test("a childless sub-agent tool row is flat (no extra disclosure)", () => {
        // The main message spawns an Agent (T1); inside it the sub-agent runs a
        // single Bash with no children. Bash must render flat — only the Agent
        // call (which HAS children) is an expandable group.
        const parent = makeMessage({
            index: 1,
            text: "MAIN",
            blockTypes: ["tool_use"],
            toolUses: [agentTool("T1")],
        });
        const child = makeMessage({
            index: 2,
            parentToolUseId: "T1",
            blockTypes: ["tool_use"],
            toolUses: [
                {
                    toolName: "Bash",
                    toolUseId: "B1",
                    summary: "echo",
                    argText: "echo hi",
                    description: null,
                    genMs: 10,
                    durationMs: 20,
                    isError: false,
                    resultPreview: "hi",
                    outputTokens: 3,
                    resultTokens: null,
                },
            ],
        });
        const { container } = render(
            <MessageTimelineSection messages={[parent, child]} />,
        );
        // Exactly two <details>: the top-level message and the Agent group.
        // The childless Bash adds none.
        expect(container.querySelectorAll("details")).toHaveLength(2);
        expect(container.querySelectorAll(".group-chevron")).toHaveLength(1);
    });

    test("renders a reconciliation row carrying the backend's unattributed tokens", () => {
        // The backend books tokens it billed but never streamed as a synthetic
        // role="reconciliation" entry; it renders as its own amber row whose
        // token cells add to the visible rows to reconcile with the run total.
        const m = makeMessage({ index: 1, cacheReadTokens: 40_000 });
        const recon = makeMessage({
            index: 2,
            role: "reconciliation",
            blockTypes: [],
            text: null,
            inputTokens: 512,
            outputTokens: 0,
            cacheWriteTokens: 0,
            cacheReadTokens: 60_000,
            note: "Tokens billed but not surfaced as a generation.",
        });
        render(<MessageTimelineSection messages={[m, recon]} />);
        expect(screen.getByText("RECONCILE")).toBeInTheDocument();
        expect(
            screen.getByText(/Tokens billed but not surfaced/),
        ).toBeInTheDocument();
        // The residual cache-read shows on the row (60k).
        expect(screen.getByText("60k")).toBeInTheDocument();
    });

    test("the reconciliation row is not counted as a message in the header", () => {
        const m = makeMessage({ index: 1 });
        const recon = makeMessage({
            index: 2,
            role: "reconciliation",
            blockTypes: [],
            text: null,
            note: "x",
        });
        render(<MessageTimelineSection messages={[m, recon]} />);
        // One real generation → "Message timeline (1)", not (2).
        expect(screen.getByText("Message timeline (1)")).toBeInTheDocument();
    });
});
