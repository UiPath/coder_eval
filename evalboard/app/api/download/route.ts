import { promises as fs } from "node:fs";
import { NextResponse } from "next/server";
import { collectTaskFiles } from "@/lib/runs";
import { DEFAULT_VARIANT_ID, isValidVariantId } from "@/lib/variants";
import { sourceById } from "@/lib/sources";
import { createZip, type ZipEntry } from "@/lib/zip";

export const dynamic = "force-dynamic";

// Bundle ONE task folder into a zip download.
//   ?run=<id>&task=<id>[&v=<variant>]  → that task's folder (<variant>/<taskId>/)
// minus the usual scaffolding noise, from the container named by ?src (the
// skills nightly when absent). In blob mode collectTaskFiles fetches the needed
// blobs first, so this mirrors what the page would load.
//
// `task` is REQUIRED. Omitting it used to mean "zip the whole run", which for a
// nightly meant ~10k blobs / ~400 MB fetched uncapped and buffered in memory
// before any of it was sent. That path and its button are gone; to inspect a
// whole run, use the blob container.
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
    if (!taskId) {
        return new NextResponse("missing task", { status: 400 });
    }

    const files = await collectTaskFiles(runId, taskId, source, variantId);
    if (!files) {
        return new NextResponse("not found", { status: 404 });
    }

    // Top-level folder inside the archive. A non-default arm is named too, so
    // two arms of the same task don't produce two identically-named zips.
    const root =
        variantId === DEFAULT_VARIANT_ID ? taskId : `${taskId}__${variantId}`;
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
