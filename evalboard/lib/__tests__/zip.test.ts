import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { createZip } from "../zip";

// Extract a zip Buffer with the system `unzip` and return a map of
// archive-relative path -> contents. Verifies our bytes against a real reader
// rather than re-parsing them with our own code.
function extract(zip: Buffer): Record<string, string> {
    const dir = mkdtempSync(path.join(tmpdir(), "evalboard-zip-"));
    try {
        const zipPath = path.join(dir, "a.zip");
        writeFileSync(zipPath, zip);
        const list = execFileSync("unzip", ["-Z1", zipPath], {
            encoding: "utf-8",
        })
            .split("\n")
            .filter((l) => l && !l.endsWith("/"));
        execFileSync("unzip", ["-o", "-q", zipPath, "-d", dir]);
        const out: Record<string, string> = {};
        for (const rel of list) {
            out[rel] = readFileSync(path.join(dir, rel), "utf-8");
        }
        return out;
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
}

describe("createZip", () => {
    test("round-trips multiple entries through the system unzip", async () => {
        const zip = await createZip([
            { name: "task-1/task.json", data: Buffer.from('{"ok":true}') },
            {
                name: "task-1/artifacts/main.py",
                data: Buffer.from("print('hi')\n"),
            },
        ]);
        const files = extract(zip);
        expect(files["task-1/task.json"]).toBe('{"ok":true}');
        expect(files["task-1/artifacts/main.py"]).toBe("print('hi')\n");
    });

    test("compresses large repetitive payloads (DEFLATE, not STORE)", async () => {
        const big = Buffer.from("a".repeat(100_000));
        const zip = await createZip([{ name: "big.txt", data: big }]);
        // Deflated output must be far smaller than the raw payload.
        expect(zip.length).toBeLessThan(big.length / 2);
        expect(extract(zip)["big.txt"]).toBe(big.toString("utf-8"));
    });

    test("handles empty files", async () => {
        const zip = await createZip([{ name: "empty", data: Buffer.alloc(0) }]);
        expect(extract(zip)["empty"]).toBe("");
    });

    test("preserves entry order and contents across many concurrently-compressed files", async () => {
        // Compression runs on the threadpool via Promise.all; this guards
        // against any reordering between the prepared array and the headers.
        const entries = Array.from({ length: 50 }, (_, i) => ({
            name: `f${i}.txt`,
            data: Buffer.from(`payload-${i}-`.repeat(500)),
        }));
        const files = extract(await createZip(entries));
        for (let i = 0; i < entries.length; i++) {
            expect(files[`f${i}.txt`]).toBe(entries[i].data.toString("utf-8"));
        }
    });
});
