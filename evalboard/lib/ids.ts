// Run / task id validators. Pure (no Node builtins), so client components and
// lib/doctor.ts can share the exact check the data layer applies; blob.ts
// re-exports them for its existing importers.

// Run / task IDs get reflected into filesystem paths and blob prefixes, so
// reject anything outside a narrow whitelist before any side effect. A run id
// of only dots ("." / "..") would name the runs dir or its parent.
const ID_RE = /^[\w.-]+$/;
const DOTS_RE = /^\.+$/;
// Task IDs from dataset-expanded tasks look like "sentiment-classification/r3"
// (suite/row). Each segment must still pass the narrow ID_RE — only the slash
// separator between segments is additionally allowed.
const TASK_ID_RE = /^[\w.-]+(\/[\w.-]+)*$/;

export function isValidId(id: unknown): id is string {
    return (
        typeof id === "string" &&
        id.length > 0 &&
        id.length < 128 &&
        ID_RE.test(id) &&
        !DOTS_RE.test(id)
    );
}

export function isValidTaskId(id: unknown): id is string {
    return (
        typeof id === "string" &&
        id.length > 0 &&
        id.length < 256 &&
        TASK_ID_RE.test(id) &&
        !id.split("/").some((s) => DOTS_RE.test(s))
    );
}
