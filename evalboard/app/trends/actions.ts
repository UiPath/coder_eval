"use server";

import {
    historyForTask,
    type TaskHistoryEntry,
    TRENDS_RECENT_RUN_COUNT,
} from "@/lib/trends";
import { parseHarnessParam } from "@/lib/harness";

export async function fetchTaskHistoryAction(
    taskId: string,
    harness: string,
): Promise<TaskHistoryEntry[]> {
    if (typeof taskId !== "string" || !taskId) return [];
    // Normalize/validate the harness (any plausible id; defaults to the primary
    // harness) so the history scope matches the trends table it expands from.
    return historyForTask(taskId, TRENDS_RECENT_RUN_COUNT, parseHarnessParam(harness));
}
