import Link from "next/link";
import { splitReason } from "@/lib/series";
import { withSource } from "@/app/_lib/source-param";

export function ReasonText({
    reason,
    sourceId,
}: {
    reason: string;
    sourceId: string;
}) {
    return (
        <>
            {splitReason(reason).map((part, i) =>
                "runId" in part ? (
                    <Link
                        key={i}
                        href={withSource(`/runs/${part.runId}`, sourceId)}
                        className="font-mono text-xs text-studio-blue hover:underline"
                    >
                        {part.runId}
                    </Link>
                ) : (
                    <span key={i}>{part.text}</span>
                ),
            )}
        </>
    );
}
