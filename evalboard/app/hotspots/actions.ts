"use server";

import { runsForTask } from "@/lib/reviews";
import {
    type TaskRunHit,
    WINDOWS,
    type Window,
} from "@/lib/reviews-types";

export async function fetchTaskDrilldownAction(
    taskId: string,
    window: Window,
): Promise<TaskRunHit[]> {
    if (typeof taskId !== "string" || !taskId) return [];
    const w: Window = WINDOWS.includes(window) ? window : "7d";
    return runsForTask(taskId, w);
}
