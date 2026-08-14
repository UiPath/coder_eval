import { promises as fs } from "node:fs";
import path from "node:path";
import { NextResponse } from "next/server";
import { resolveSafePath } from "@/lib/runs";
import { sourceById } from "@/lib/sources";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
    const url = new URL(req.url);
    const runId = url.searchParams.get("run");
    const relPath = url.searchParams.get("path");
    // Artifact paths are relative to the SOURCE's cache dir (readTaskDetail's
    // artifactPrefix), so without ?src a Scribe artifact resolves under the
    // skills cache and 404s — or, worse, hits a same-named file there.
    const source = sourceById(url.searchParams.get("src"));
    if (!runId || !relPath) {
        return new NextResponse("missing run or path", { status: 400 });
    }

    const abs = await resolveSafePath(runId, relPath, source);
    if (!abs) {
        return new NextResponse("forbidden", { status: 403 });
    }

    const buf = await fs.readFile(abs).catch(() => null);
    if (!buf) {
        return new NextResponse("not found", { status: 404 });
    }

    const ext = path.extname(abs).toLowerCase();
    const contentType =
        ext === ".json" || ext === ".flow" || ext === ".uiproj"
            ? "application/json"
            : ext === ".log"
              ? "text/plain; charset=utf-8"
              : "application/octet-stream";

    return new NextResponse(buf, {
        status: 200,
        headers: {
            "Content-Type": contentType,
            "Content-Disposition": `attachment; filename="${path.basename(abs)}"`,
        },
    });
}
