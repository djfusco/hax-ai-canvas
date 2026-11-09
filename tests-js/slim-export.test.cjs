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
