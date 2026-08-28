import { promises as fs } from "node:fs";
import { NextResponse } from "next/server";
import { collectRunFiles, collectTaskFiles } from "@/lib/runs";
import { DEFAULT_VARIANT_ID, isValidVariantId } from "@/lib/variants";
import { sourceById } from "@/lib/sources";
import { createZip, type ZipEntry } from "@/lib/zip";

export const dynamic = "force-dynamic";

// Bundle a task folder, or a whole run, into a zip download.
//   ?run=<id>&task=<id>[&v=<variant>]  → that task's folder (<variant>/<taskId>/)
//   ?run=<id>                          → the entire run folder (run.json + every task dir)
// minus the usual scaffolding noise, from the container named by ?src (the
// skills nightly when absent). In blob mode the collect* helpers fetch the
// needed blobs first, so this mirrors what the page would load.
export async function GET(req: Request) {
    const url = new URL(req.url);
    const runId = url.searchParams.get("run");
    const taskId = url.searchParams.get("task");
    // Experiment arm. Absent / malformed → the single arm a non-experiment run
    // writes, so pre-variant download URLs resolve unchanged.
    const v = url.searchParams.get("v");
    const variantId = isValidVariantId(v) ? v : DEFAULT_VARIANT_ID;
    const source = sourceById(url.searchParams.get("src"));
    if (!runId) {
        return new NextResponse("missing run", { status: 400 });
    }

    const files = taskId
        ? await collectTaskFiles(runId, taskId, source, variantId)
        : await collectRunFiles(runId, source);
    if (!files) {
        return new NextResponse("not found", { status: 404 });
    }

    // Top-level folder inside the archive: the task id for a task download,
    // the run id for a whole-run download. A non-default arm is named too, so
    // two arms of the same task don't produce two identically-named zips.
    const root = taskId
        ? variantId === DEFAULT_VARIANT_ID
            ? taskId
            : `${taskId}__${variantId}`
        : runId;
    const entries: ZipEntry[] = [];
    for (const f of files) {
        const data = await fs.readFile(f.abs).catch(() => null);
        if (data) entries.push({ name: `${root}/${f.relPath}`, data });
    }
    if (entries.length === 0) {
        return new NextResponse("not found", { status: 404 });
    }

    const zip = await createZip(entries);
    // Wrap in a fresh Uint8Array so the body type narrows to BodyInit
    // (Buffer.concat is typed Buffer<ArrayBufferLike>, which NextResponse rejects).
    return new NextResponse(new Uint8Array(zip), {
        status: 200,
        headers: {
            "Content-Type": "application/zip",
            "Content-Disposition": `attachment; filename="${root}.zip"`,
            "Content-Length": String(zip.length),
        },
    });
}
