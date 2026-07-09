"use server";

import { historyForTask, type TaskHistoryEntry } from "@/lib/trends";

export async function fetchTaskHistoryAction(
    taskId: string,
): Promise<TaskHistoryEntry[]> {
    if (typeof taskId !== "string" || !taskId) return [];
    return historyForTask(taskId);
}
