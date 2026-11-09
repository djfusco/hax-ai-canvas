'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const SlimExport = require('../python/app/static/slim-export.js');

const ROOT = path.resolve(__dirname, '..');
const PYTHON = process.env.PYTHON || 'python3';

function python(code, args = []) {
  return execFileSync(PYTHON, ['-c', code, ...args], { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
}

// Canvas-like export with a large media entry placed first, so its bytes are
// far from the table of contents at the end of the file.
const MAKE_EXPORT = String.raw`
import json, sys, zipfile
sys.path.insert(0, "tests")
from test_slim_import import text_entries
out = sys.argv[1]
media = bytes(range(256)) * (3 * 1024 * 1024 // 256)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr(zipfile.ZipInfo("web_resources/lecture one.mp4"), media)  # stored
    z.writestr("web_resources/reading.pdf", b"%PDF-1.4 synthetic reading")
    for name, data in text_entries().items():
        z.writestr(name, data)
with zipfile.ZipFile(out) as z:
    info = z.getinfo("web_resources/lecture one.mp4")
    print(json.dumps({"start": info.header_offset + 30 + len(info.filename.encode()),
                      "length": info.compress_size}))
`;

function tracked(blob) {
  const ranges = [];
  return {
    ranges,
    size: blob.size,
    slice(start = 0, end = blob.size) { ranges.push([start, end]); return blob.slice(start, end); },
  };
}

async function fileBlob(file) {
  return fs.openAsBlob(file);
}

function tmp(name) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'slim-'));
  return path.join(dir, name);
}

test('never reads media bytes and keeps every text entry', async () => {
  const exportPath = tmp('course.imscc');
  const media = JSON.parse(python(MAKE_EXPORT, [exportPath]));
  const file = tracked(await fileBlob(exportPath));
  const scan = await SlimExport.scanExport(file);
  const mediaEnd = media.start + media.length;
  for (const [start, end] of file.ranges) {
    assert.ok(end <= media.start || start >= mediaEnd, `read ${start}-${end} overlaps media ${media.start}-${mediaEnd}`);
  }
  const names = scan.kept.map(k => k.name);
  assert.ok(names.includes('imsmanifest.xml'));
  assert.ok(names.includes('course_settings/rubrics.xml'));
  assert.ok(names.includes('gessay/assignment_settings.xml'));
  const video = scan.inventory.find(m => m.path === 'web_resources/lecture one.mp4');
  assert.equal(video.bytes, 3 * 1024 * 1024);
  assert.equal(video.mime, 'video/mp4');
  assert.deepEqual(scan.documents.map(d => d.path), ['web_resources/reading.pdf']);
  assert.ok(scan.stats.skippedBytes >= 3 * 1024 * 1024);
});

test('slim package imports in Python with full metadata', async () => {
  const exportPath = tmp('course.imscc');
  python(MAKE_EXPORT, [exportPath]);
  const file = await fileBlob(exportPath);
  const scan = await SlimExport.scanExport(file);
  const { blob, summary } = await SlimExport.buildSlimPackage(file, scan, {
    includeDocuments: ['web_resources/reading.pdf'],
  });
  assert.ok(summary.slimBytes < summary.archiveBytes / 10, 'slim package is much smaller');
  assert.equal(summary.documentsIncluded, 1);
  const slimPath = tmp('slim.zip');
  fs.writeFileSync(slimPath, Buffer.from(await blob.arrayBuffer()));
  const result = JSON.parse(python(String.raw`
import json, sys, zipfile
sys.path.insert(0, "python")
with zipfile.ZipFile(sys.argv[1]) as z:
    assert z.testzip() is None
    names = z.namelist()
    inventory = json.loads(z.read("media_inventory.json"))
import slim_import
model = slim_import.import_slim(sys.argv[1])
print(json.dumps({"names": names, "inventory": inventory, "counts": model["provenance"]["counts"],
                  "rubrics": len(model["rubrics"]), "weighted": model["course"]["weighted_groups"]}))
`, [slimPath]));
  assert.ok(result.names.includes('materials/reading.pdf'));
  assert.ok(!result.names.includes('web_resources/lecture one.mp4'));
  assert.deepEqual(result.inventory.map(m => m.path), ['web_resources/lecture one.mp4']);
  assert.equal(result.counts.assignments, 3);
  assert.equal(result.counts.media, 1);
  assert.equal(result.rubrics, 1);
  assert.equal(result.weighted, true);
});

test('reads ZIP64 archives', async () => {
  const zipPath = tmp('zip64.imscc');
  python(String.raw`
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("imsmanifest.xml", "<manifest/>")
    for n in range(65540):
        z.writestr(f"web_resources/i{n}.png", b"")
`, [zipPath]);
  const scan = await SlimExport.scanExport(await fileBlob(zipPath));
  assert.equal(scan.stats.entries, 65541);
  assert.deepEqual(scan.kept.map(k => k.name), ['imsmanifest.xml']);
  assert.equal(scan.inventory.length, 65540);
});

test('rejects files that are not Canvas exports', async () => {
  await assert.rejects(SlimExport.scanExport(new Blob(['not a zip at all, definitely'])), /not a Canvas course export/);
  const zip = await SlimExport.writeZip([{ name: 'readme.txt', data: new TextEncoder().encode('hi') }]);
  await assert.rejects(SlimExport.scanExport(zip), /imsmanifest\.xml is missing/);
  const unsafe = await SlimExport.writeZip([
    { name: 'imsmanifest.xml', data: new Uint8Array(1) },
    { name: '../evil.html', data: new Uint8Array(1) },
  ]);
  await assert.rejects(SlimExport.scanExport(unsafe), /unsafe path/);
});

test('detects corrupted entries', async () => {
  const zip = await SlimExport.writeZip([{ name: 'imsmanifest.xml', data: new TextEncoder().encode('x'.repeat(500)) }]);
  const bytes = new Uint8Array(await zip.arrayBuffer());
  bytes[30 + 'imsmanifest.xml'.length + 2] ^= 0xFF; // inside the compressed data
  await assert.rejects(SlimExport.scanExport(new Blob([bytes])), /integrity|malformed|beyond|invalid|Decompression/i);
});

test('rejects documents that were not offered and honours cancel', async () => {
  const exportPath = tmp('course.imscc');
  python(MAKE_EXPORT, [exportPath]);
  const file = await fileBlob(exportPath);
  const scan = await SlimExport.scanExport(file);
  await assert.rejects(
    SlimExport.buildSlimPackage(file, scan, { includeDocuments: ['web_resources/lecture one.mp4'] }),
    /not offered/);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(SlimExport.scanExport(file, { signal: controller.signal }), /cancelled/);
});

test('writeZip output round-trips through the reader', async () => {
  const data = new TextEncoder().encode('<manifest>' + 'a'.repeat(10000) + '</manifest>');
  const zip = await SlimExport.writeZip([{ name: 'imsmanifest.xml', data }]);
  const scan = await SlimExport.scanExport(zip);
  assert.deepEqual(scan.kept[0].data, data);
  assert.ok(zip.size < data.length, 'text is compressed');
});

test('handles courses re-zipped in a folder, macOS metadata and Windows separators', async () => {
  const enc = t => new TextEncoder().encode(t);
  const nested = await SlimExport.writeZip([
    { name: 'My Course/imsmanifest.xml', data: enc('<manifest/>') },
    { name: 'My Course/wiki_content/a.html', data: enc('<p>a</p>') },
    { name: 'My Course/.DS_Store', data: enc('x') },
    { name: '__MACOSX/My Course/._a.html', data: enc('x') },
  ]);
  const scan = await SlimExport.scanExport(nested);
  assert.deepEqual(scan.kept.map(k => k.name).sort(), ['imsmanifest.xml', 'wiki_content/a.html']);
  assert.deepEqual(scan.inventory, []);
  const windows = await SlimExport.writeZip([
    { name: 'imsmanifest.xml', data: enc('<manifest/>') },
    { name: 'res001\\page.html', data: enc('<p>x</p>') },
  ]);
  assert.ok((await SlimExport.scanExport(windows)).kept.some(k => k.name === 'res001/page.html'));
  const twoCourses = await SlimExport.writeZip([
    { name: 'A/imsmanifest.xml', data: enc('<manifest/>') },
    { name: 'B/imsmanifest.xml', data: enc('<manifest/>') },
  ]);
  await assert.rejects(SlimExport.scanExport(twoCourses), /imsmanifest\.xml is missing/);
  const escape = await SlimExport.writeZip([
    { name: 'imsmanifest.xml', data: enc('<manifest/>') },
    { name: 'a\\..\\..\\evil.html', data: enc('x') },
  ]);
  await assert.rejects(SlimExport.scanExport(escape), /unsafe path/);
});

// A virtual 5 GiB export: the video is never materialized; reading it fails the test.
function virtualZip64() {
  const enc = t => new TextEncoder().encode(t);
  const segments = [];
  let offset = 0;
  const push = (bytes, length = bytes.length) => { segments.push({ start: offset, length, bytes }); offset += length; };
  const u16 = (v, n) => { v.setUint16(n[0], n[1], true); };
  const header = (fields, size) => { const b = new DataView(new ArrayBuffer(size)); for (const [at, value, width] of fields) {
    if (width === 2) b.setUint16(at, value, true); else if (width === 8) { b.setUint32(at, value % 2 ** 32, true); b.setUint32(at + 4, Math.floor(value / 2 ** 32), true); } else b.setUint32(at, value, true); }
    return new Uint8Array(b.buffer); };
  const HUGE = 5 * 1024 ** 3;
  const entries = [];
  // Local header for the huge stored video, sizes in a ZIP64 extra field.
  const videoName = enc('web_resources/lecture.mp4');
  entries.push({ name: videoName, offset, size: HUGE, crc: 0 });
  push(header([[0, 0x04034b50], [4, 45, 2], [14, 0], [18, 0xFFFFFFFF], [22, 0xFFFFFFFF], [26, videoName.length, 2], [28, 20, 2]], 30));
  push(videoName);
  push(header([[0, 1, 2], [2, 16, 2], [4, HUGE, 8], [12, HUGE, 8]], 20));
  push(null, HUGE); // never read
  for (const [name, text] of [['imsmanifest.xml', '<manifest identifier="m"><resources/></manifest>'],
    ['wiki_content/after-4gb.html', '<p>Beyond the 4 GiB mark.</p>']]) {
    const data = enc(text), bytes = enc(name);
    entries.push({ name: bytes, offset, size: data.length, crc: SlimExport.crc32(data) });
    push(header([[0, 0x04034b50], [4, 20, 2], [14, SlimExport.crc32(data)], [18, data.length], [22, data.length], [26, bytes.length, 2]], 30));
    push(bytes); push(data);
  }
  const cdStart = offset;
  for (const e of entries) {
    const big = e.size > 0xFFFFFFF0, far = e.offset > 0xFFFFFFF0;
    const extra = [];
    if (big) extra.push(e.size, e.size);
    if (far) extra.push(e.offset);
    const extraBytes = extra.length ? header([[0, 1, 2], [2, extra.length * 8, 2], ...extra.map((v, i) => [4 + i * 8, v, 8])], 4 + extra.length * 8) : new Uint8Array(0);
    push(header([[0, 0x02014b50], [4, 45, 2], [6, 45, 2], [16, e.crc], [20, big ? 0xFFFFFFFF : e.size], [24, big ? 0xFFFFFFFF : e.size],
      [28, e.name.length, 2], [30, extraBytes.length, 2], [42, far ? 0xFFFFFFFF : e.offset]], 46));
    push(e.name); push(extraBytes);
  }
  const cdSize = offset - cdStart, zip64End = offset;
  push(header([[0, 0x06064b50], [4, 44, 8], [12, 45, 2], [14, 45, 2], [24, entries.length, 8], [32, entries.length, 8], [40, cdSize, 8], [48, cdStart, 8]], 56));
  push(header([[0, 0x07064b50], [8, zip64End, 8], [16, 1]], 20));
  push(header([[0, 0x06054b50], [8, 0xFFFF, 2], [10, 0xFFFF, 2], [12, 0xFFFFFFFF], [16, 0xFFFFFFFF]], 22));
  const reads = [];
  const media = segments.find(s => s.bytes === null);
  return {
    reads, media, size: offset,
    slice(start, end = offset) {
      reads.push([start, end]);
      if (start < media.start + media.length && end > media.start) throw new Error(`read media bytes ${start}-${end}`);
      const parts = [];
      for (const s of segments) {
        const a = Math.max(start, s.start), b = Math.min(end, s.start + s.length);
        if (a < b) parts.push(s.bytes.subarray(a - s.start, b - s.start));
      }
      return new Blob(parts);
    },
  };
}

test('reads a 5 GiB ZIP64 export with course text beyond 4 GiB, without touching the video', async () => {
  const file = virtualZip64();
  assert.ok(file.size > 5 * 1024 ** 3);
  const scan = await SlimExport.scanExport(file);
  const page = scan.kept.find(k => k.name === 'wiki_content/after-4gb.html');
  assert.equal(new TextDecoder().decode(page.data), '<p>Beyond the 4 GiB mark.</p>');
  assert.equal(scan.inventory[0].bytes, 5 * 1024 ** 3);
  const bytesRead = file.reads.reduce((sum, [a, b]) => sum + (b - a), 0);
  assert.ok(bytesRead < 64 * 1024, `read ${bytesRead} bytes of a ${file.size}-byte export`);
  const { blob } = await SlimExport.buildSlimPackage(file, scan);
  assert.ok(blob.size < 4096);
});
