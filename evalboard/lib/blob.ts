import { promises as fs } from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";
import { DefaultAzureCredential } from "@azure/identity";
import {
    BlobServiceClient,
    RestError,
    type ContainerClient,
} from "@azure/storage-blob";

const ACCOUNT = "coderevaltests";
const CONTAINER = "runs";

// Run / task IDs get reflected into filesystem paths and blob prefixes, so
// reject anything outside a narrow whitelist before any side effect.
const ID_RE = /^[\w.-]+$/;

export function isValidId(id: unknown): id is string {
    return (
        typeof id === "string" &&
        id.length > 0 &&
        id.length < 128 &&
        ID_RE.test(id)
    );
}

function assertValidId(id: string, label: string): void {
    if (!isValidId(id)) {
        throw new Error(`Invalid ${label}: ${JSON.stringify(id)}`);
    }
}

function isNotFound(err: unknown): boolean {
    return err instanceof RestError && err.statusCode === 404;
}

let containerClient: ContainerClient | null = null;

function getContainer(): ContainerClient {
    if (containerClient) return containerClient;
    const url = `https://${ACCOUNT}.blob.core.windows.net`;
    const svc = new BlobServiceClient(url, new DefaultAzureCredential());
    containerClient = svc.getContainerClient(CONTAINER);
    return containerClient;
}

export async function listRunIdsRemote(): Promise<string[]> {
    const c = getContainer();
    const ids: string[] = [];
    for await (const item of c.listBlobsByHierarchy("/")) {
        if (item.kind === "prefix") {
            const id = item.name.replace(/\/$/, "");
            if (id !== "latest") ids.push(id);
        }
    }
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
        await getContainer().getBlobClient(blobName).downloadToFile(tmpPath);
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

export async function ensureRunAnalysis(
    runId: string,
    destRoot: string,
): Promise<void> {
    assertValidId(runId, "runId");
    return dedupe(`analysis:${runId}`, async () => {
        try {
            await downloadBlob(`${runId}/analysis.md`, destRoot);
        } catch (err) {
            if (!isNotFound(err)) throw err;
        }
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
    assertValidId(taskId, "taskId");
    return dedupe(`task:${runId}/${taskId}`, async () => {
        await ensureRunSummary(runId, destRoot);
        const c = getContainer();
        const ops: Promise<void>[] = [];
        // `listBlobsFlat` recurses, so both the flat legacy layout
        // (`default/<task>/task.json`) and the nested replicate layout
        // (`default/<task>/00/task.json`) download unchanged — the prefix
        // scope is the task subtree either way. `resolveTaskContentDir` in
        // runs.ts then picks the right shape at render time.
        const prefix = `${runId}/default/${taskId}/`;
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
