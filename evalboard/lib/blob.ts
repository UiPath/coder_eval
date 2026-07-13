import { promises as fs } from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";
import type { ContainerClient } from "@azure/storage-blob";

const ACCOUNT = "coderevaltests";
const CONTAINER = "runs";

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

let containerClient: ContainerClient | null = null;

// The Azure SDK is loaded lazily (and lives under optionalDependencies) so
// local-mode readers — and OSS users who `pnpm install --no-optional` — never
// pull @azure/*. This only runs in remote mode, where every caller awaits it.
async function getContainer(): Promise<ContainerClient> {
    if (containerClient) return containerClient;
    const { BlobServiceClient } = await import("@azure/storage-blob");
    const { DefaultAzureCredential } = await import("@azure/identity");
    const url = `https://${ACCOUNT}.blob.core.windows.net`;
    const svc = new BlobServiceClient(url, new DefaultAzureCredential());
    containerClient = svc.getContainerClient(CONTAINER);
    return containerClient;
}

export async function listRunIdsRemote(): Promise<string[]> {
    if (LOCAL_RUNS_DIR) return listRunIdsLocal(LOCAL_RUNS_DIR);
    const c = await getContainer();
    const ids: string[] = [];
    for await (const item of c.listBlobsByHierarchy("/")) {
        if (item.kind === "prefix") {
            const id = item.name.replace(/\/$/, "");
            if (id !== "latest") ids.push(id);
        }
    }
    return ids.sort().reverse();
}

// In local mode, only directories with a run.json count as runs — this drops
// the `latest` symlink and any half-written run dirs that the eval framework
// created but never populated.
async function listRunIdsLocal(root: string): Promise<string[]> {
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
        const c = await getContainer();
        await c.getBlobClient(blobName).downloadToFile(tmpPath);
        await fs.rename(tmpPath, localPath);
    } catch (err) {
        await fs.unlink(tmpPath).catch(() => {});
        throw err;
    }
}

// Collapse concurrent fetches of the same run so we don't hammer blob
// or race on the same files.
const inFlight = new Map<string, Promise<void>>();

function dedupe(key: string, fn: () => Promise<void>): Promise<void> {
    const existing = inFlight.get(key);
    if (existing) return existing;
    const p = fn().finally(() => inFlight.delete(key));
    inFlight.set(key, p);
    return p;
}

export async function ensureRunSummary(
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`summary:${runId}`, async () => {
        // Only 404 is "run not uploaded yet" — readers observe an absent
        // file on disk. Auth, network, and IMDS failures must propagate so
        // they don't masquerade as "not found" in the UI.
        try {
            await downloadBlob(`${runId}/run.json`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

// The activation suite is a nested sub-run: its self-contained run.json (cases +
// rollup) lives at <runId>/activation/run.json. The activation card and page read
// it via this fetch; absent (404) on runs without an activation suite.
export async function ensureActivationSummary(
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`activation:${runId}`, async () => {
        try {
            await downloadBlob(`${runId}/activation/run.json`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

export async function ensureRunAnalysis(
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`analysis:${runId}`, async () => {
        try {
            await downloadBlob(`${runId}/analysis.md`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

// Optional run metadata sidecar (title / description / adhoc), written by
// `dashboard upload --title/--description/--adhoc`. Absent on pipeline runs
// and any run uploaded before this feature — 404 is swallowed like analysis.
export async function ensureRunMeta(
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`meta:${runId}`, async () => {
        try {
            await downloadBlob(`${runId}/meta.json`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
    });
}

export async function ensureRunReviewIndex(
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`review-index:${runId}`, async () => {
        try {
            await downloadBlob(`${runId}/review_index.json`, destRoot);
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
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`run:${runId}`, async () => {
        const c = await getContainer();
        const ops: Promise<void>[] = [];
        const prefix = `${runId}/`;
        for await (const blob of c.listBlobsFlat({ prefix })) {
            if (blob.name.includes("/.venv/")) continue;
            ops.push(downloadBlob(blob.name, destRoot));
        }
        await Promise.all(ops);
    });
}

// Narrow fetch: run.json + just one task subdir. Used by the per-task
// detail page so opening a deep link to a 50-task run doesn't pull every
// task's artifacts.
export async function ensureTaskDir(
    runId: string,
    taskId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    assertValidTaskId(taskId, "taskId");
    if (LOCAL_RUNS_DIR) return;
    return dedupe(`task:${runId}/${taskId}`, async () => {
        // Activation cases live in the nested sub-run (<runId>/activation/...),
        // so their row + per-case dir come from there; skills tasks from the
        // top-level run. Fetch the matching run.json for the row lookup.
        const activation = taskId.startsWith("skill-activation/");
        if (activation) await ensureActivationSummary(runId, destRoot);
        else await ensureRunSummary(runId, destRoot);
        const c = await getContainer();
        const ops: Promise<void>[] = [];
        // `listBlobsFlat` recurses, so both the flat legacy layout
        // (`default/<task>/task.json`) and the nested replicate layout
        // (`default/<task>/00/task.json`) download unchanged — the prefix
        // scope is the task subtree either way. `resolveTaskContentDir` in
        // runs.ts then picks the right shape at render time.
        const prefix = activation
            ? `${runId}/activation/default/${taskId}/`
            : `${runId}/default/${taskId}/`;
        for await (const blob of c.listBlobsFlat({ prefix })) {
            // Agent sandboxes that run Python leave a `.venv/` tree (hundreds
            // of files, tens of MB) under the task dir. No UI page reads it,
            // so skipping it keeps task-detail loads from stalling on the
            // initial prefetch.
            if (blob.name.includes("/.venv/")) continue;
            ops.push(downloadBlob(blob.name, destRoot));
        }
        await Promise.all(ops);
    });
}
