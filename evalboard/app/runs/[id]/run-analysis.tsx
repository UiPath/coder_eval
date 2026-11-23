import type { AnalysisPlan } from "@/lib/doctor";
import { AnalysisPanel } from "./analysis-panel";
import { TaskFailureAnalysis } from "./task-failure-analysis";

/**
 * With the doctor block, the AI summary, the block and the rest of analysis.md
 * are one panel; without it, the AI summary panel alone.
 */
export function RunAnalysis({
    plan,
    runId,
    sourceId,
}: {
    plan: AnalysisPlan;
    runId: string;
    sourceId: string;
}) {
    if (plan.doctor)
        return (
            <TaskFailureAnalysis
                doctor={plan.doctor}
                runId={runId}
                sourceId={sourceId}
                summary={plan.summary}
                rest={plan.rest}
            />
        );
    return plan.summary ? <AnalysisPanel markdown={plan.summary} /> : null;
}
