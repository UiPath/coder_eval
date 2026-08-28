import { promises as fs } from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";
import type { BlobServiceClient, ContainerClient } from "@azure/storage-blob";
import { DEFAULT_VARIANT_ID, isValidVariantId } from "./variants";

const ACCOUNT = "coderevaltests";

// When set, evalboard reads runs from this local directory and never touches
// blob — listing comes from the filesystem and ensure* becomes a no-op.
// Lib/runs.ts also resolves RUNS_DIR to this path so readers see the files
// directly.
export const LOCAL_RUNS_DIR = process.env.EVALBOARD_LOCAL_RUNS_DIR || null;

// Run / task IDs get reflected into filesystem paths and blob prefixes, so
// reject anything outside a narrow whitelist before any side effect.
const ID_RE = /^[\w.-]+$/;
// Task IDs from dataset-expanded tasks look like "sentiment-classification/r3"
// (suite/row). Each segment must still pass the narrow ID_RE — only the slash
// separator between segments is additionally allowed.
const TASK_ID_RE = /^[\w.-]+(\/[\w.-]+)*$/;

export function isValidId(id: unknown): id is string {
    return (
        typeof id === "string" &&
        id.length > 0 &&
        id.length < 128 &&
        ID_RE.test(id)
    );
}

export function isValidTaskId(id: unknown): id is string {
    return (
        typeof id === "string" &&
        id.length > 0 &&
        id.length < 256 &&
        TASK_ID_RE.test(id) &&
        !id.split("/").some((s) => s === "." || s === "..")
    );
}

function assertValidId(id: string, label: string): void {
    if (!isValidId(id)) {
        throw new Error(`Invalid ${label}: ${JSON.stringify(id)}`);
    }
}

function assertValidVariantId(id: string, label: string): void {
    if (!isValidVariantId(id)) {
        throw new Error(`Invalid ${label}: ${JSON.stringify(id)}`);
    }
}

function assertValidTaskId(id: string, label: string): void {
    if (!isValidTaskId(id)) {
        throw new Error(`Invalid ${label}: ${JSON.stringify(id)}`);
    }
}

// Duck-typed 404 check. Avoids a runtime import of `RestError` from
// @azure/storage-blob so local-mode readers never load the Azure SDK — the
// blob client and its error type only arrive via the dynamic import in
// getContainer(). Azure's RestError carries a numeric `statusCode`.
function isNotFound(err: unknown): boolean {
    return (
        typeof err === "object" &&
        err !== null &&
        "statusCode" in err &&
        (err as { statusCode?: number }).statusCode === 404
    );
}

// One client per container — evalboard serves several sources (see lib/sources.ts)
// from a single deployment, so this can't be a single module-level client.
const containerClients = new Map<string, ContainerClient>();

// ...but ONE service client behind them all. getContainerClient is a pure
// factory off it, so building a fresh BlobServiceClient (and a fresh
// DefaultAzureCredential) per container would duplicate managed-identity token
// acquisition for no benefit.
let servicePromise: Promise<BlobServiceClient> | null = null;

// The Azure SDK is loaded lazily (and lives under optionalDependencies) so
// local-mode readers — and OSS users who `pnpm install --no-optional` — never
// pull @azure/*. This only runs in remote mode, where every caller awaits it.
async function getService(): Promise<BlobServiceClient> {
    // Memoize the PROMISE, not the resolved client: concurrent first calls
    // (every source's page can render at once) would otherwise each construct
    // their own credential before the first one finished.
    servicePromise ??= (async () => {
        const { BlobServiceClient } = await import("@azure/storage-blob");
        const { DefaultAzureCredential } = await import("@azure/identity");
        const url = `https://${ACCOUNT}.blob.core.windows.net`;
        return new BlobServiceClient(url, new DefaultAzureCredential());
    })();
    return servicePromise;
}

async function getContainer(container: string): Promise<ContainerClient> {
    const cached = containerClients.get(container);
    if (cached) return cached;
    const svc = await getService();
    const client = svc.getContainerClient(container);
    containerClients.set(container, client);
    return client;
}

// Remote-only: local mode is resolved by listRunIds in runs.ts, which owns
// RUNS_DIR and therefore the per-source root. Listing here off the bare
// LOCAL_RUNS_DIR used to ignore `container` entirely, so /scribe reported the
// SKILLS tree's run ids under an "aria-runs" label while every read resolved
// under the -scribe sibling — the exact cross-container leak this module's
// container threading exists to prevent, just on the local backend.
export async function listRunIdsRemote(container: string): Promise<string[]> {
    const c = await getContainer(container);
    const ids: string[] = [];
    try {
        for await (const item of c.listBlobsByHierarchy("/")) {
            if (item.kind === "prefix") {
                const id = item.name.replace(/\/$/, "");
                if (id !== "latest") ids.push(id);
            }
        }
    } catch (err) {
        // A container that hasn't been created yet 404s here, and with nothing
        // catching it the page hits the root error boundary — which tells the
        // viewer "this is usually transient" about a state that is permanent
        // until someone creates the container. Empty is the honest answer; the
        // caller's empty state says so. Only 404: auth failures, IMDS problems,
        // and 5xx must still surface rather than render as "no runs".
        if (!isNotFound(err)) throw err;
        return [];
    }
    return ids.sort().reverse();
}

// In local mode, only directories with a run.json count as runs — this drops
// the `latest` symlink and any half-written run dirs that the eval framework
// created but never populated.
export async function listRunIdsLocal(root: string): Promise<string[]> {
    const entries = await fs
        .readdir(root, { withFileTypes: true })
        .catch(() => []);
    const ids: string[] = [];
    await Promise.all(
        entries.map(async (e) => {
            if (!e.isDirectory()) return;
            if (e.name === "latest") return;
            if (!isValidId(e.name)) return;
            if (await exists(path.join(root, e.name, "run.json"))) {
                ids.push(e.name);
            }
        }),
    );
    return ids.sort().reverse();
}

async function exists(p: string): Promise<boolean> {
    try {
        await fs.access(p);
        return true;
    } catch {
        return false;
    }
}

async function downloadBlob(
    container: string,
    blobName: string,
    destRoot: string,
): Promise<void> {
    const localPath = path.join(destRoot, blobName);
    if (await exists(localPath)) return;
    await fs.mkdir(path.dirname(localPath), { recursive: true });
    // Download to a unique temp path then rename so a crash or aborted
    // request never leaves a partial file that `exists()` would treat as
    // cached. The random suffix keeps concurrent writers from clobbering
    // each other's temp file.
    const tmpPath = `${localPath}.${randomBytes(6).toString("hex")}.tmp`;
    try {
        const c = await getContainer(container);
        await c.getBlobClient(blobName).downloadToFile(tmpPath);
        await fs.rename(tmpPath, localPath);
    } catch (err) {
        await fs.unlink(tmpPath).catch(() => {});
        throw err;
    }
}

// Collapse concurrent fetches of the same run so we don't hammer blob
// or race on the same files.
//
// Every key is container-scoped: run ids are only unique within a container
// (the skills and aria suites both name runs `YYYY-MM-DD_HH-MM-SS`), so a
// container-blind key would let a fetch for one source satisfy a concurrent
// fetch for a different source's identically-named run.
const inFlight = new Map<string, Promise<void>>();

function dedupe(key: string, fn: () => Promise<void>): Promise<void> {
    const existing = inFlight.get(key);
    if (existing) return existing;
    const p = fn().finally(() => inFlight.delete(key));
    inFlight.set(key, p);
    return p;
}

export async function ensureRunSummary(
    container: string,
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`summary:${container}:${runId}`, async () => {
        // Only 404 is "run not uploaded yet" — readers observe an absent
        // file on disk. Auth, network, and IMDS failures must propagate so
        // they don't masquerade as "not found" in the UI.
        try {
            await downloadBlob(container, `${runId}/run.json`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

// The activation suite is a nested sub-run: its self-contained run.json (cases +
// rollup) lives at <runId>/activation/run.json. The activation card and page read
// it via this fetch; absent (404) on runs without an activation suite.
export async function ensureActivationSummary(
    container: string,
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`activation:${container}:${runId}`, async () => {
        try {
            await downloadBlob(
                container,
                `${runId}/activation/run.json`,
                destRoot,
            );
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

export async function ensureRunAnalysis(
    container: string,
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`analysis:${container}:${runId}`, async () => {
        try {
            await downloadBlob(container, `${runId}/analysis.md`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

// Optional run metadata sidecar (title / description / adhoc), written by
// `dashboard upload --title/--description/--adhoc`. Absent on pipeline runs
// and any run uploaded before this feature — 404 is swallowed like analysis.
export async function ensureRunMeta(
    container: string,
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`meta:${container}:${runId}`, async () => {
        try {
            await downloadBlob(container, `${runId}/meta.json`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

export async function ensureRunReviewIndex(
    container: string,
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`review-index:${container}:${runId}`, async () => {
        try {
            await downloadBlob(container, `${runId}/review_index.json`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

// Full fetch: every blob under the run prefix. Used by the "download whole
// run" button, which needs all task subdirs at once (the narrow per-task /
// summary fetches only cache what their page reads). `.venv` trees are
// skipped for the same reason as ensureTaskDir — no page reads them and they
// dwarf the real deliverables.
export async function ensureRunDir(
    container: string,
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`run:${container}:${runId}`, async () => {
        const c = await getContainer(container);
        const ops: Promise<void>[] = [];
        const prefix = `${runId}/`;
        for await (const blob of c.listBlobsFlat({ prefix })) {
            if (blob.name.includes("/.venv/")) continue;
            ops.push(downloadBlob(container, blob.name, destRoot));
        }
        await Promise.all(ops);
    });
}

// Narrow fetch: run.json + just one task subdir. Used by the per-task
// detail page so opening a deep link to a 50-task run doesn't pull every
// task's artifacts.
export async function ensureTaskDir(
    container: string,
    runId: string,
    taskId: string,
    destRoot: string,
    // Which arm of a multi-variant run to fetch. Defaults to the single arm a
    // non-experiment run writes, so every existing call site is unchanged.
    variantId: string = DEFAULT_VARIANT_ID,
): Promise<void> {
    assertValidId(runId, "runId");
    assertValidTaskId(taskId, "taskId");
    assertValidVariantId(variantId, "variantId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`task:${container}:${runId}/${variantId}/${taskId}`, async () => {
        // Activation cases live in the nested sub-run (<runId>/activation/...),
        // so their row + per-case dir come from there; skills tasks from the
        // top-level run. Fetch the matching run.json for the row lookup.
        const activation = taskId.startsWith("skill-activation/");
        if (activation)
            await ensureActivationSummary(container, runId, destRoot);
        else await ensureRunSummary(container, runId, destRoot);
        const c = await getContainer(container);
        const ops: Promise<void>[] = [];
        // `listBlobsFlat` recurses, so both the flat legacy layout
        // (`<variant>/<task>/task.json`) and the nested replicate layout
        // (`<variant>/<task>/00/task.json`) download unchanged — the prefix
        // scope is the task subtree either way. `resolveTaskContentDir` in
        // runs.ts then picks the right shape at render time.
        //
        // The activation sub-run is single-variant by construction (it is a
        // nested run of its own), so it keeps the literal `default` segment.
        const prefix = activation
            ? `${runId}/activation/${DEFAULT_VARIANT_ID}/${taskId}/`
            : `${runId}/${variantId}/${taskId}/`;
        for await (const blob of c.listBlobsFlat({ prefix })) {
            // Agent sandboxes that run Python leave a `.venv/` tree (hundreds
            // of files, tens of MB) under the task dir. No UI page reads it,
            // so skipping it keeps task-detail loads from stalling on the
            // initial prefetch.
            if (blob.name.includes("/.venv/")) continue;
            ops.push(downloadBlob(container, blob.name, destRoot));
        }
        await Promise.all(ops);
    });
}
