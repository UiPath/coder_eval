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
        mixedGenMs: null,
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
                    execStartMs: null,
                    execEndMs: null,
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
                    execStartMs: null,
                    execEndMs: null,
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
            execStartMs: null,
            execEndMs: null,
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
                    execStartMs: null,
                    execEndMs: null,
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
                    execStartMs: null,
                    execEndMs: null,
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

// The strip must reconcile: generation + tool exec are shown against the wall
// clock they should add up to, so a harness that stops reporting one of them
// is visible on the page instead of silently reading as fast.
describe("MessageTimelineSection — Unaccounted cell", () => {
    // Each summary cell is <label><value>[<sub-grid>]. Read the value element
    // itself so a matching string elsewhere in the strip (the Generation
    // sub-cells also render times and percentages) cannot satisfy an
    // assertion about this cell.
    function cell(label: string): HTMLElement {
        const parent = screen.getByText(label).parentElement as HTMLElement;
        return parent.children[1] as HTMLElement;
    }

    function renderStrip(
        taskDurationSeconds: number | null | undefined,
        overrides: Partial<MessageEvent> = {},
    ) {
        const m = makeMessage({
            generationMs: 4000,
            textMs: 4000,
            toolUses: [
                {
                    toolName: "Bash",
                    toolUseId: "tu_1",
                    summary: "ls",
                    argText: "ls",
                    description: null,
                    genMs: null,
                    durationMs: 1000,
                    isError: false,
                    resultPreview: null,
                    outputTokens: null,
                    resultTokens: null,
                    // BOUNDED. `toolExecutionMs` unions bounded intervals and
                    // drops a bare duration, matching the Python selector, so
                    // a durationMs-only tool would contribute 0 here.
                    execStartMs: 0,
                    execEndMs: 1000,
                },
            ],
            ...overrides,
        });
        return render(
            <MessageTimelineSection
                messages={[m]}
                taskDurationSeconds={taskDurationSeconds}
            />,
        );
    }

    test("renders the residual and its share of the wall clock", () => {
        // 10s wall clock − 4s generation − 1s tool exec = 5s (50%).
        renderStrip(10);
        expect(cell("Unaccounted").textContent).toBe("5.0s (50%)");
    });

    test("the four pre-existing cells still render their values", () => {
        renderStrip(10);
        expect(cell("Messages").textContent).toBe("1");
        expect(cell("Generation").textContent).toBe("4.0s");
        expect(cell("Tool exec").textContent).toBe("1.0s");
        expect(cell("Slow events").textContent).toBe("0 gen · 0 tool");
    });

    test("a residual at or above 25% is tinted red", () => {
        // 5s of 10s = 50%.
        renderStrip(10);
        expect(cell("Unaccounted").className).toContain("text-red-700");
    });

    test("a residual below 25% is not tinted", () => {
        // 5s gen+tool of 5.5s wall clock ≈ 9%.
        renderStrip(5.5);
        expect(cell("Unaccounted").textContent).toBe("500ms (9%)");
        expect(cell("Unaccounted").className).not.toContain("text-red-700");
    });

    test("no recorded duration renders an em-dash, no NaN and no percentage", () => {
        const { container } = renderStrip(undefined);
        expect(cell("Unaccounted").textContent).toBe("—");
        expect(container.textContent).not.toContain("NaN");
    });

    test("a zero-second task shows the raw residual with no percentage", () => {
        const { container } = renderStrip(0);
        expect(cell("Unaccounted").textContent).toBe("-5.0s");
        expect(container.textContent).not.toContain("NaN");
    });

    test("a negative residual renders signed and is NOT tinted red", () => {
        // Overlap (parallel tools, or a tool closing inside a generation
        // window) is a signal, not unreported time — so it is neither clamped
        // nor flagged. The share stays signed too: 5s accounted against a 1s
        // task is a 4x overlap, and saying so beats hiding it.
        renderStrip(1);
        expect(cell("Unaccounted").textContent).toBe("-4.0s (-400%)");
        expect(cell("Unaccounted").className).not.toContain("text-red-700");
        // Its own colour: overlap is a different fact from a large positive
        // residual, and identical grey to a healthy row would hide it.
        expect(cell("Unaccounted").className).toContain("text-amber-700");
    });

    test("a sub-agent is not counted as both generation and parent tool time", () => {
        // The Agent call's durationMs already spans the sub-agent's whole run,
        // so adding the sub-agent's own generation on top double-counts it.
        const agentCall = {
            toolName: "Agent",
            toolUseId: "tu_agent",
            summary: "spawn",
            argText: null,
            description: null,
            genMs: null,
            durationMs: 6000,
            isError: false,
            resultPreview: null,
            outputTokens: null,
            resultTokens: null,
            execStartMs: 0,
            execEndMs: 6000,
        };
        const main = makeMessage({
            index: 1,
            generationMs: 1000,
            textMs: 1000,
            toolUses: [agentCall],
        });
        const child = makeMessage({
            index: 2,
            generationMs: 5000,
            textMs: 5000,
            parentToolUseId: "tu_agent",
        });
        render(
            <MessageTimelineSection messages={[main, child]} taskDurationSeconds={10} />,
        );
        // 10s − 1s main generation − 6s Agent call = 3s. Counting the child's
        // 5s of generation too would report -2s.
        expect(cell("Unaccounted").textContent).toBe("3.0s (30%)");
        expect(cell("Generation").textContent).toBe("1.0s");
        expect(cell("Tool exec").textContent).toBe("6.0s");
    });

    test("concurrent tools are counted once, so the residual stays honest", () => {
        // Two 5s sleeps started 1s apart occupy 6s of wall clock, not 10s.
        // Summing their durations reported -615ms on a live task where the
        // honest answer was positive: sandbox setup and grading.
        const span = (start: number, end: number) => ({
            toolName: "Bash",
            toolUseId: `tu_${start}`,
            summary: "sleep 5",
            argText: "sleep 5",
            description: null,
            genMs: null,
            durationMs: end - start,
            isError: false,
            resultPreview: null,
            outputTokens: null,
            resultTokens: null,
            execStartMs: start,
            execEndMs: end,
        });
        render(
            <MessageTimelineSection
                messages={[
                    makeMessage({
                        generationMs: 1000,
                        textMs: 1000,
                        toolUses: [span(1_000, 6_000), span(2_000, 7_000)],
                    }),
                ]}
                taskDurationSeconds={9.5}
            />,
        );
        expect(cell("Tool exec").textContent).toBe("6.0s");
        // 9.5s − 1s generation − 6s of tool wall clock = 2.5s.
        expect(cell("Unaccounted").textContent).toBe("2.5s (26%)");
    });

    test("a tool with no recorded bounds contributes nothing", () => {
        // The policy both languages now share: a duration with no start and
        // end cannot be placed on the timeline, so it cannot be unioned with
        // anything — folding it in double-books whatever it overlapped. Python
        // has always dropped it (`main_thread_tool_spans` filters on
        // `is not None`); this cell used to add it. Its time is not lost, it
        // moves into Unaccounted, which is what that cell means.
        renderStrip(10, {
            toolUses: [
                {
                    toolName: "Bash",
                    toolUseId: "tu_unbounded",
                    summary: "ls",
                    argText: "ls",
                    description: null,
                    genMs: null,
                    durationMs: 1000,
                    isError: false,
                    resultPreview: null,
                    outputTokens: null,
                    resultTokens: null,
                    execStartMs: null,
                    execEndMs: null,
                },
            ],
        });
        expect(cell("Tool exec").textContent).toBe("0ms");
        // 10s − 4s generation − 0s tool exec: the second is the point.
        expect(cell("Unaccounted").textContent).toBe("6.0s (60%)");
    });

    test("the cell explains that the residual is not only agent time", () => {
        renderStrip(10);
        expect(
            screen.getByText("Unaccounted").parentElement,
        ).toHaveAttribute("title", expect.stringContaining("post_run"));
    });

    test("it no longer claims to hold the setup phase, which has its own cell", () => {
        // The residual used to name sandbox setup as one of its contents, and
        // that was ~1.9s of known, constant orchestrator cost on every row —
        // a named phase hiding inside a bucket called "unaccounted".
        renderStrip(10);
        const title = screen
            .getByText("Unaccounted")
            .parentElement!.getAttribute("title")!;
        expect(title).not.toContain("sandbox setup");
    });

    test("setup and grading are subtracted out of the residual", () => {
        render(
            <MessageTimelineSection
                messages={[makeMessage({ generationMs: 1000, textMs: 1000 })]}
                taskDurationSeconds={10}
                setupMs={2000}
                gradingMs={500}
            />,
        );
        expect(cell("Setup").textContent).toBe("2.0s");
        expect(cell("Grading").textContent).toBe("500ms");
        // 10s − 1s generation − 2s setup − 0.5s grading = 6.5s.
        expect(cell("Unaccounted").textContent).toBe("6.5s (65%)");
    });

    test("a run predating the fields leaves their time IN the residual", () => {
        // The whole point of subtracting only what was measured: an absent
        // field must not be silently taken off as a zero, and must not turn
        // the residual into a different number than the run used to publish.
        render(
            <MessageTimelineSection
                messages={[makeMessage({ generationMs: 1000, textMs: 1000 })]}
                taskDurationSeconds={10}
            />,
        );
        expect(cell("Setup").textContent).toBe("—");
        expect(cell("Grading").textContent).toBe("—");
        expect(cell("Unaccounted").textContent).toBe("9.0s (90%)");
    });
});

describe("MessageTimelineSection — a row's EXEC cell", () => {
    function span(start: number, end: number) {
        return {
            toolName: "Bash",
            toolUseId: `tu_${start}`,
            summary: "sleep",
            argText: "sleep",
            description: null,
            genMs: null,
            durationMs: end - start,
            isError: false,
            resultPreview: null,
            outputTokens: null,
            resultTokens: null,
            execStartMs: start,
            execEndMs: end,
        };
    }

    // The message row lays out GEN then EXEC as the first two numeric spans of
    // its own grid; `:scope >` keeps expanded tool sub-rows out of the match.
    function execOf(container: HTMLElement): string {
        // The message row is `ol > li > details > summary`, laying out
        // #, GEN, EXEC as its first three numeric spans. `:scope >` keeps the
        // expanded tool sub-rows inside the <details> body out of the match.
        const row = container.querySelector("ol > li > details > summary") as HTMLElement;
        const nums = row.querySelectorAll(":scope > span.tabular-nums");
        return nums[2]?.textContent ?? "";
    }

    function execCell(toolUses: ReturnType<typeof span>[]): string {
        const { container } = render(
            <MessageTimelineSection
                messages={[makeMessage({ generationMs: 1000, textMs: 1000, toolUses })]}
            />,
        );
        return execOf(container);
    }

    test("concurrent calls count their overlap ONCE", () => {
        // The bug this replaced: two `sleep 2` Bash calls overlapping almost
        // entirely rendered 4.1s for 2.1s of wall clock — more tool time in
        // one message than the whole task's Tool exec cell, which is
        // impossible on its face.
        // union 0->3000 = 3.0s; the sum of the two durations would be 4.0s.
        expect(execCell([span(0, 2_000), span(1_000, 3_000)])).toBe("3.0s");
    });

    test("sequential calls still add up, so the row reconciles with its parts", () => {
        // Expanding the row shows each call's own wall clock. When they did
        // not overlap, those add to this number; when they did, they do not,
        // and that difference is the concurrency.
        expect(execCell([span(0, 1_000), span(2_000, 3_000)])).toBe("2.0s");
    });

    test("it agrees with the header for a single message", () => {
        const toolUses = [span(0, 2_000), span(1_000, 3_000)];
        const { container } = render(
            <MessageTimelineSection
                messages={[makeMessage({ generationMs: 1000, textMs: 1000, toolUses })]}
                taskDurationSeconds={10}
            />,
        );
        const header = screen.getByText("Tool exec").parentElement!.querySelectorAll("div")[1];
        expect(execOf(container)).toBe(header.textContent);
    });
});

describe("MessageTimelineSection — Startup and Teardown cells", () => {
    function cell(label: string): HTMLElement {
        const parent = screen.getByText(label).parentElement as HTMLElement;
        return parent.children[1] as HTMLElement;
    }

    // Same 4s generation + 1s tool exec fixture the Unaccounted block uses, so
    // the two blocks' numbers are directly comparable.
    function renderStrip(props: {
        taskDurationSeconds?: number | null;
        harnessStartupMs?: number | null;
        harnessTeardownMs?: number | null;
        storedToolMs?: number | null;
    }) {
        const m = makeMessage({
            generationMs: 4000,
            textMs: 4000,
            toolUses: [
                {
                    toolName: "Bash",
                    toolUseId: "tu_1",
                    summary: "ls",
                    argText: "ls",
                    description: null,
                    genMs: null,
                    durationMs: 1000,
                    isError: false,
                    resultPreview: null,
                    outputTokens: null,
                    resultTokens: null,
                    // BOUNDED. `toolExecutionMs` unions bounded intervals and
                    // drops a bare duration, matching the Python selector, so
                    // a durationMs-only tool would contribute 0 here.
                    execStartMs: 0,
                    execEndMs: 1000,
                },
            ],
        });
        return render(<MessageTimelineSection messages={[m]} {...props} />);
    }

    test("both buckets render their measured value", () => {
        renderStrip({
            taskDurationSeconds: 10,
            harnessStartupMs: 3000,
            harnessTeardownMs: 1500,
        });
        expect(cell("Startup").textContent).toBe("3.0s");
        expect(cell("Teardown").textContent).toBe("1.5s");
    });

    test("a measured zero renders as 0ms, not as an em-dash", () => {
        // A head of 0 stays representable: a turn can reach its first model
        // output with nothing measurable in front of it, and a clamped
        // inversion is still a measurement because both ends were observed.
        // "—" would report that as a missing one.
        renderStrip({
            taskDurationSeconds: 10,
            harnessStartupMs: 0,
            harnessTeardownMs: 834.7,
        });
        expect(cell("Startup").textContent).toBe("0ms");
        expect(cell("Teardown").textContent).toBe("835ms");
    });

    test("Unaccounted shrinks by exactly startup + teardown", () => {
        // 10s − 4s gen − 1s tool = 5s before; minus 3s + 1.5s = 500ms after.
        renderStrip({
            taskDurationSeconds: 10,
            harnessStartupMs: 3000,
            harnessTeardownMs: 1500,
        });
        expect(cell("Unaccounted").textContent).toBe("500ms (5%)");
    });

    test("a corrected residual still above 25% stays red", () => {
        // The other direction: naming the buckets must not disable the tint,
        // only move the number it reads. 20s − 4s gen − 1s tool − 3s − 1s
        // = 11s, still 55% unexplained.
        renderStrip({
            taskDurationSeconds: 20,
            harnessStartupMs: 3000,
            harnessTeardownMs: 1000,
        });
        expect(cell("Unaccounted").textContent).toBe("11.0s (55%)");
        expect(cell("Unaccounted").className).toContain("text-red-700");
    });

    test("a residual that was red goes grey once the buckets are named", () => {
        // The 25% threshold applies to the CORRECTED residual: 50% before,
        // 5% after, so the red tint must follow the correction.
        renderStrip({
            taskDurationSeconds: 10,
            harnessStartupMs: 3000,
            harnessTeardownMs: 1500,
        });
        expect(cell("Unaccounted").className).not.toContain("text-red-700");
    });

    test("an older run with neither field renders — and today's residual", () => {
        const { container } = renderStrip({ taskDurationSeconds: 10 });
        expect(cell("Startup").textContent).toBe("—");
        expect(cell("Teardown").textContent).toBe("—");
        // Byte-identical to the pre-existing Unaccounted expectation.
        expect(cell("Unaccounted").textContent).toBe("5.0s (50%)");
        expect(cell("Unaccounted").className).toContain("text-red-700");
        expect(container.textContent).not.toContain("NaN");
    });

    test("only the present bucket is subtracted", () => {
        renderStrip({ taskDurationSeconds: 10, harnessStartupMs: 3000 });
        expect(cell("Startup").textContent).toBe("3.0s");
        expect(cell("Teardown").textContent).toBe("—");
        expect(cell("Unaccounted").textContent).toBe("2.0s (20%)");
    });

    test("the residual still goes negative and stays amber", () => {
        // Naming the buckets does not clamp the overlap signal.
        renderStrip({
            taskDurationSeconds: 5,
            harnessStartupMs: 1000,
            harnessTeardownMs: 500,
        });
        expect(cell("Unaccounted").textContent).toBe("-1.5s (-30%)");
        expect(cell("Unaccounted").className).toContain("text-amber-700");
    });

    test("the stored tool bucket is preferred over recomputing it", () => {
        // The collector wrote `tool_union_ms` from the same span set it
        // measured the head and the tail against, so reading it is how this
        // cell and the harness are guaranteed to agree. The fixture's own
        // messages would compute 1.0s, so a number that is not 2.5s proves the
        // stored value was ignored.
        renderStrip({ taskDurationSeconds: 10, storedToolMs: 2500 });
        expect(cell("Tool exec").textContent).toBe("2.5s");
    });

    test("a stored measured zero wins over the fallback", () => {
        // `0` is a measurement: spans were recorded and occupied no measurable
        // time. Coalescing it away would silently replace it with the 1.0s the
        // messages compute.
        renderStrip({ taskDurationSeconds: 10, storedToolMs: 0 });
        expect(cell("Tool exec").textContent).toBe("0ms");
    });

    test("a run predating the field falls back to the message stream", () => {
        renderStrip({ taskDurationSeconds: 10 });
        expect(cell("Tool exec").textContent).toBe("1.0s");
    });

    test("each bucket says what it measures and that it is not decomposed", () => {
        renderStrip({
            taskDurationSeconds: 10,
            harnessStartupMs: 3000,
            harnessTeardownMs: 1500,
        });
        expect(screen.getByText("Startup").parentElement).toHaveAttribute(
            "title",
            expect.stringContaining("time-to-first-token"),
        );
        expect(screen.getByText("Teardown").parentElement).toHaveAttribute(
            "title",
            expect.stringContaining("teardown"),
        );
    });
});

// A mixed-kind emission's per-kind split is apportioned by content size, so
// the page must say so and must not let the unattributable part distort the
// thinking share.
describe("MessageTimelineSection — mixed generation sub-cell", () => {
    function cellValue(label: string): string {
        const parent = screen.getByText(label).parentElement as HTMLElement;
        return (parent.children[1] as HTMLElement).textContent ?? "";
    }

    test("a mixed message renders the mixed sub-cell with its value", () => {
        render(
            <MessageTimelineSection
                messages={[
                    makeMessage({
                        generationMs: 1000,
                        thinkingMs: 100,
                        textMs: null,
                        toolGenMs: null,
                        mixedGenMs: 900,
                    }),
                ]}
            />,
        );
        expect(screen.getByText("unsplit")).toBeInTheDocument();
        expect(cellValue("unsplit")).toContain("900ms");
    });

    test("a fully attributed message renders no mixed sub-cell at all", () => {
        // claude-code's shape: the layout must look exactly as it does today.
        render(
            <MessageTimelineSection
                messages={[
                    makeMessage({
                        generationMs: 1000,
                        thinkingMs: null,
                        textMs: 1000,
                        toolGenMs: null,
                        mixedGenMs: null,
                    }),
                ]}
            />,
        );
        expect(screen.queryByText("unsplit")).toBeNull();
    });

    test("the thinking tint is computed against the ATTRIBUTABLE part", () => {
        // 100ms of thinking out of 100ms attributable is 100% — red — even
        // though it is only 10% of the raw generation total.
        render(
            <MessageTimelineSection
                messages={[
                    makeMessage({
                        generationMs: 1000,
                        thinkingMs: 100,
                        textMs: null,
                        toolGenMs: null,
                        mixedGenMs: 900,
                    }),
                ]}
            />,
        );
        const thinking = screen.getByText("thinking").parentElement as HTMLElement;
        expect((thinking.children[1] as HTMLElement).className).toContain("text-red-700");
    });

    test("a low thinking share against a fully attributed total is not tinted", () => {
        render(
            <MessageTimelineSection
                messages={[
                    makeMessage({
                        generationMs: 1000,
                        thinkingMs: 100,
                        textMs: null,
                        toolGenMs: 900,
                        mixedGenMs: null,
                    }),
                ]}
            />,
        );
        const thinking = screen.getByText("thinking").parentElement as HTMLElement;
        expect((thinking.children[1] as HTMLElement).className).not.toContain("text-red-700");
    });

    test("the four sub-cell percentages sum to 100%, not 190%", () => {
        // The tint uses the ATTRIBUTABLE denominator; the displayed shares
        // must not, or thinking reads as 100% of a generation it was 10% of
        // — on exactly the emission class this phase stops over-crediting.
        render(
            <MessageTimelineSection
                messages={[
                    makeMessage({
                        generationMs: 1000,
                        thinkingMs: 100,
                        textMs: null,
                        toolGenMs: null,
                        mixedGenMs: 900,
                    }),
                ]}
            />,
        );
        const pct = (label: string) => {
            const cell = screen.getByText(label).parentElement as HTMLElement;
            const m = /\((-?\d+)%\)/.exec(cell.textContent ?? "");
            return m ? Number(m[1]) : 0;
        };
        expect(pct("thinking")).toBe(10);
        expect(pct("tool args") + pct("text") + pct("unsplit") + pct("thinking")).toBe(100);
    });

    test("the split row says the mixed split is an estimate", () => {
        // The caveat travels with the SPLIT, which is what it qualifies — the
        // Generation total above it is measured, not apportioned.
        render(<MessageTimelineSection messages={[makeMessage()]} />);
        expect(
            screen.getByText("Generation split").parentElement,
        ).toHaveAttribute(
            "title",
            expect.stringContaining("apportioned by content size"),
        );
    });
});
