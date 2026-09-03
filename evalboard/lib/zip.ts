import { crc32, deflateRaw } from "node:zlib";
import { promisify } from "node:util";

// Async DEFLATE so compression runs on libuv's threadpool instead of blocking
// the event loop — a whole-run download can be tens of MB across many files,
// and a sync deflate would stall every other request the server is serving
// for the duration.
const deflateRawAsync = promisify(deflateRaw);

// Minimal ZIP archive writer. Avoids pulling in a third-party dependency
// (jszip/archiver) for the single use case of bundling a task folder for
// download. Produces a standard PKZIP archive: per-entry local file headers +
// DEFLATE-compressed (or STORE'd) data, a central directory, and an
// end-of-central-directory record. No ZIP64, so the practical ceiling is 4 GB
// per file and 65 535 entries — far above any task folder.

export interface ZipEntry {
    // Path inside the archive (forward slashes). Must be relative.
    name: string;
    data: Buffer;
}

// Standard CRC-32 (IEEE 802.3 polynomial 0xEDB88320), one per ZIP entry.
// `zlib.crc32` computes it natively; the hand-rolled loop it replaces walked the
// uncompressed bytes one at a time on the event loop, ~47x slower measured.
// `>>> 0` keeps it unsigned, matching what that loop returned.
function entryCrc(buf: Buffer): number {
    return crc32(buf) >>> 0;
}

// Convert a JS Date to the DOS date/time fields ZIP uses. Seconds have 2 s
// resolution (the low bit is dropped) — that's the format, not a bug.
function dosDateTime(d: Date): { date: number; time: number } {
    const year = Math.max(d.getFullYear() - 1980, 0);
    const date = (year << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
    const time =
        (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
    return { date: date & 0xffff, time: time & 0xffff };
}

// Bit 11 of the general-purpose flags marks the filename as UTF-8.
const FLAG_UTF8 = 0x0800;

// Compress with raw DEFLATE; fall back to STORE when compression doesn't help
// (already-compressed payloads can grow). method: 8 = deflate, 0 = store.
async function compress(
    data: Buffer,
): Promise<{ method: number; body: Buffer }> {
    if (data.length === 0) return { method: 0, body: data };
    const deflated = await deflateRawAsync(data);
    return deflated.length < data.length
        ? { method: 8, body: deflated }
        : { method: 0, body: data };
}

export async function createZip(
    entries: ZipEntry[],
    now: Date = new Date(),
): Promise<Buffer> {
    const { date, time } = dosDateTime(now);

    // Compress (and CRC) every entry up front, concurrently on the threadpool.
    // Promise.all preserves order, so the archive layout below stays
    // deterministic. CRC runs after the await so it's spread across ticks too.
    const prepared = await Promise.all(
        entries.map(async (entry) => {
            const { method, body } = await compress(entry.data);
            return {
                nameBuf: Buffer.from(entry.name, "utf-8"),
                crc: entryCrc(entry.data),
                method,
                body,
                uncompressedSize: entry.data.length,
            };
        }),
    );

    const localParts: Buffer[] = [];
    const centralParts: Buffer[] = [];
    let offset = 0;

    for (const { nameBuf, crc, method, body, uncompressedSize } of prepared) {
        const local = Buffer.alloc(30);
        local.writeUInt32LE(0x04034b50, 0); // local file header signature
        local.writeUInt16LE(20, 4); // version needed
        local.writeUInt16LE(FLAG_UTF8, 6); // general purpose flags
        local.writeUInt16LE(method, 8); // compression method
        local.writeUInt16LE(time, 10);
        local.writeUInt16LE(date, 12);
        local.writeUInt32LE(crc, 14);
        local.writeUInt32LE(body.length, 18); // compressed size
        local.writeUInt32LE(uncompressedSize, 22); // uncompressed size
        local.writeUInt16LE(nameBuf.length, 26);
        local.writeUInt16LE(0, 28); // extra field length
        localParts.push(local, nameBuf, body);

        const central = Buffer.alloc(46);
        central.writeUInt32LE(0x02014b50, 0); // central dir header signature
        central.writeUInt16LE(20, 4); // version made by
        central.writeUInt16LE(20, 6); // version needed
        central.writeUInt16LE(FLAG_UTF8, 8);
        central.writeUInt16LE(method, 10);
        central.writeUInt16LE(time, 12);
        central.writeUInt16LE(date, 14);
        central.writeUInt32LE(crc, 16);
        central.writeUInt32LE(body.length, 20);
        central.writeUInt32LE(uncompressedSize, 24);
        central.writeUInt16LE(nameBuf.length, 28);
        central.writeUInt16LE(0, 30); // extra field length
        central.writeUInt16LE(0, 32); // comment length
        central.writeUInt16LE(0, 34); // disk number start
        central.writeUInt16LE(0, 36); // internal attributes
        central.writeUInt32LE(0, 38); // external attributes
        central.writeUInt32LE(offset, 42); // local header offset
        centralParts.push(central, nameBuf);

        offset += local.length + nameBuf.length + body.length;
    }

    const centralDir = Buffer.concat(centralParts);
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0); // end of central directory signature
    eocd.writeUInt16LE(0, 4); // disk number
    eocd.writeUInt16LE(0, 6); // disk with central directory
    eocd.writeUInt16LE(entries.length, 8); // entries on this disk
    eocd.writeUInt16LE(entries.length, 10); // total entries
    eocd.writeUInt32LE(centralDir.length, 12); // central directory size
    eocd.writeUInt32LE(offset, 16); // central directory offset
    eocd.writeUInt16LE(0, 20); // comment length

    return Buffer.concat([...localParts, centralDir, eocd]);
}
