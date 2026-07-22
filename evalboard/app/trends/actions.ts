"use server";

import { historyForTask, type TaskHistoryEntry } from "@/lib/trends";
import { TRENDS_RECENT_RUN_COUNT } from "@/lib/trends";
import { KNOWN_HARNESSES } from "@/app/_components/harness-badge";

export async function fetchTaskHistoryAction(
    taskId: string,
    harness: string,
): Promise<TaskHistoryEntry[]> {
    if (typeof taskId !== "string" || !taskId) return [];
    // Validate against the known set so a spoofed value can't widen the scan;
    // fall back to the default harness (see trends aggregation).
    const h = (KNOWN_HARNESSES as readonly string[]).includes(harness)
        ? harness
        : "claude-code";
    return historyForTask(taskId, TRENDS_RECENT_RUN_COUNT, h);
}
