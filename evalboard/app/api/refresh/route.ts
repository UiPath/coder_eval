import { revalidateTag } from "next/cache";
import { NextResponse } from "next/server";
import { LOCAL_RUNS_DIR } from "@/lib/blob";
import { runCacheTag } from "@/lib/overview";
import { RUNS_DIR, clearRunCacheDir } from "@/lib/runs";
import { runsDirFor, sourceById } from "@/lib/sources";

export const dynamic = "force-dynamic";

// Evict a single run's on-disk blob cache so the next view re-downloads it
// fresh from storage. The existence-based cache in lib/blob.ts never re-fetches
// a file once it is on disk, so a run's mutable sidecars (title/description in
// meta.json, reviews, analysis) edited in blob after the run was first viewed
// stay stale forever. This is the manual escape hatch. Single-run only by
// design — orphan cleanup for deleted/renamed runs is out of scope.
//
//   POST /api/refresh?run=<run-id>[&src=<source-id>]
export async function POST(req: Request) {
    // Local mode points RUNS_DIR at the real coder_eval runs dir (the source of
    // truth, not a cache); deleting from it would destroy run data.
    if (LOCAL_RUNS_DIR) {
        return NextResponse.json(
            { error: "refresh is disabled in local mode" },
            { status: 400 },
        );
    }
    const params = new URL(req.url).searchParams;
    const runId = params.get("run");
    if (!runId) {
        return NextResponse.json({ error: "missing run" }, { status: 400 });
    }
    // Each source caches into its own subtree, and a run id can exist in more
    // than one; clearing the base dir would evict a DIFFERENT run and leave the
    // requested one stale forever.
    const source = sourceById(params.get("src"));
    // clearRunCacheDir is the single id validator (rejects separators, `.`,
    // `..`); a false return means the id was unsafe, never "not found" — an
    // uncached run deletes to a harmless no-op.
    if (!(await clearRunCacheDir(runsDirFor(RUNS_DIR, source), runId))) {
        return NextResponse.json({ error: "invalid run" }, { status: 400 });
    }
    // Evicting the on-disk copy is only half the job: the front page reads a
    // memoized PROJECTION of it (lib/overview.ts::cachedLoadPerRunFor), and a
    // settled run's projection is held for a day. Drop that entry too, or the
    // refresh silently does nothing for every run older than 24h.
    revalidateTag(runCacheTag(source.id, runId));
    // Re-download is lazy on next render. The run page is force-dynamic and
    // reads the sidecars straight from disk, so it is fresh immediately.
    return new Response(null, { status: 204 });
}
