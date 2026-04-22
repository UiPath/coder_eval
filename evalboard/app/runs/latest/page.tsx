import { notFound, redirect } from "next/navigation";
import { latestRunId } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function LatestRun() {
    const id = await latestRunId();
    if (!id) notFound();
    redirect(`/runs/${id}`);
}
