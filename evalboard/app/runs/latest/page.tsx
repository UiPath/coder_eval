import { notFound, redirect } from "next/navigation";
import { latestRunId } from "@/lib/runs";
import { sourceById } from "@/lib/sources";
import { scalarParam, withSource } from "@/app/_lib/source-param";

export const dynamic = "force-dynamic";

export default async function LatestRun({
    searchParams,
}: {
    searchParams: Promise<{ src?: string | string[] }>;
}) {
    // `/runs/latest?src=scribe` means "the newest run in THAT container" — the
    // source has to survive the redirect or the run page reads the wrong one.
    const source = sourceById(scalarParam((await searchParams).src));
    const id = await latestRunId(source);
    if (!id) notFound();
    redirect(withSource(`/runs/${id}`, source.id));
}
