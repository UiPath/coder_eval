"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Tame heading sizes so the analysis nests visually under the panel header
// instead of dwarfing the page-level "Run" h1 above it. Pure CSS — no
// assumption about the markdown's structure.
const PROSE_CLASSES = [
    "prose",
    "prose-sm",
    "max-w-none",
    "prose-headings:text-gray-900",
    "prose-h1:text-base",
    "prose-h2:text-base",
    "prose-h3:text-sm",
    "prose-h4:text-sm",
    "prose-h2:mt-6",
    "prose-h3:mt-4",
    "prose-table:text-xs",
    "prose-th:text-left",
    "prose-th:font-semibold",
    "prose-td:align-top",
    "analysis-prose",
].join(" ");

export function AnalysisPanel({ markdown }: { markdown: string }) {
    const [open, setOpen] = useState(false);

    return (
        <section className="border border-studio-blue/30 bg-studio-blue/5 rounded-lg">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                className="w-full px-4 py-3 flex items-center justify-between gap-3 text-left hover:bg-studio-blue/10 rounded-lg transition-colors"
            >
                <span className="text-xs font-semibold text-studio-blue uppercase tracking-wide">
                    AI summary
                </span>
                <span className="text-xs text-studio-blue whitespace-nowrap">
                    {open ? "Hide" : "Read analysis →"}
                </span>
            </button>
            {open && (
                <div
                    className={`border-t border-studio-blue/20 bg-white px-5 py-4 ${PROSE_CLASSES}`}
                >
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {markdown}
                    </ReactMarkdown>
                </div>
            )}
        </section>
    );
}
