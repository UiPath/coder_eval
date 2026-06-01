"use client";

import { useEffect, useState } from "react";

// Shared column-help bubble. A small ⓘ next to a column header opens a static,
// selectable popover explaining what the number is, what drives it up, and how
// to bring it down. Used by the run-page task grid (TaskGrid) and the task-page
// message timeline so the two stay consistent.
export type ColHelp = {
    title: string;
    body: string;
    causes?: string; // common causes of high values
    fix?: string; // potential fixes
};

// Token-column help shared verbatim across pages. Wording is page-neutral (no
// "this task" / "this call") so it reads correctly on both the per-task grid
// and the per-message timeline.
export const TOKEN_COLUMN_HELP = {
    output: {
        title: "Output tokens",
        body: "Text, code, tool arguments and reasoning the model generated.",
        causes: "verbose final answers, large file rewrites, heavy reasoning.",
        fix: "ask for concise output, scope edits to smaller diffs, cap max_output_tokens / max_turns.",
    },
    cw: {
        title: "Cache-write tokens",
        body: "Context newly written into the prompt cache (cache_creation_input_tokens).",
        causes: "the cached prefix keeps changing — new files read mid-run, a growing transcript — so it's re-written instead of reused.",
        fix: "keep stable content (system prompt, skills, instructions) at the front of the prompt; don't inject volatile content early; reuse sessions.",
    },
    cr: {
        title: "Cache-read tokens",
        body: "Cached input re-billed on every later call (cache_read_input_tokens). Usually the dominant cost line.",
        causes: "large context (big files, long transcript, many skills/tools) replayed on every call.",
        fix: "put less in context (smaller file reads, fewer files), shorten the run (fewer turns), trim system/skill payloads, compact long transcripts.",
    },
} satisfies Record<string, ColHelp>;

export function HelpPopover({
    help,
    align,
}: {
    help: ColHelp;
    align: "left" | "right";
}) {
    return (
        <div
            role="tooltip"
            // Anchor under the ⓘ; align to the same edge as the column text so
            // it stays inside the table on the right-aligned columns.
            className={`absolute top-full z-20 mt-1.5 w-72 cursor-auto rounded-md border border-gray-200 bg-white p-3 text-left text-xs font-normal leading-snug text-gray-600 shadow-lg ${
                align === "right" ? "right-0" : "left-0"
            }`}
            // Keep clicks inside the card from sorting / closing.
            onClick={(e) => e.stopPropagation()}
        >
            <div className="font-semibold text-gray-900">{help.title}</div>
            <p className="mt-1">{help.body}</p>
            {help.causes && (
                <p className="mt-2">
                    <span className="font-medium text-gray-700">
                        Common causes:
                    </span>{" "}
                    {help.causes}
                </p>
            )}
            {help.fix && (
                <p className="mt-1">
                    <span className="font-medium text-gray-700">Reduce by:</span>{" "}
                    {help.fix}
                </p>
            )}
        </div>
    );
}

// Self-contained ⓘ icon + popover with its own open state (click to toggle,
// click-outside / Escape to close). Drop-in for headers that aren't managed by
// a parent's shared open-state — e.g. the message-timeline header, which is a
// server component. The click-outside check keys off the `[data-col-help]`
// wrapper, so clicking a different icon closes this one (one open at a time).
export function ColHelpIcon({
    help,
    align = "right",
}: {
    help: ColHelp;
    align?: "left" | "right";
}) {
    const [open, setOpen] = useState(false);
    useEffect(() => {
        if (!open) return;
        const onDown = (e: MouseEvent) => {
            const el = e.target as Element | null;
            if (!el?.closest("[data-col-help]")) setOpen(false);
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key === "Escape") setOpen(false);
        };
        document.addEventListener("mousedown", onDown);
        document.addEventListener("keydown", onKey);
        return () => {
            document.removeEventListener("mousedown", onDown);
            document.removeEventListener("keydown", onKey);
        };
    }, [open]);
    return (
        <span data-col-help className="relative inline-flex">
            <button
                type="button"
                aria-label={`What is ${help.title}?`}
                aria-expanded={open}
                onClick={() => setOpen((o) => !o)}
                className={`flex h-4 w-4 items-center justify-center rounded-full border text-[10px] font-semibold leading-none transition-colors ${
                    open
                        ? "border-studio-blue text-studio-blue"
                        : "border-gray-300 text-gray-400 hover:border-gray-400 hover:text-gray-600"
                }`}
            >
                i
            </button>
            {open && <HelpPopover help={help} align={align} />}
        </span>
    );
}
