/*
 * Slim a Canvas course export in the browser.
 *
 * A Canvas .imscc export is a ZIP archive. ZIP keeps its table of contents
 * (the central directory) at the end of the file, so we read that table,
 * then read only the text entries the consultant needs (manifest, HTML, XML,
 * QTI, course_settings) with ranged reads. Videos and other media are never
 * read: they are listed in media_inventory.json by path, size and type.
 *
 * No dependencies. Uses Blob.slice, DecompressionStream / CompressionStream
 * ('deflate-raw') and TextDecoder, available in current browsers and Node 22.
 *
 *   const scan = await SlimExport.scanExport(file, {onProgress, signal});
 *   // show scan.documents to the instructor; let them tick some
 *   const {blob, summary} = await SlimExport.buildSlimPackage(file, scan,
 *       {includeDocuments: ['web_resources/syllabus.pdf'], signal});
 */
(function (root) {
  'use strict';

  const MiB = 1024 * 1024;
  const LIMITS = {
    maxEntryBytes: 2 * MiB,          // per kept text entry (uncompressed)
    maxKeptBytes: 32 * MiB,          // all kept text entries
    maxDocumentBytes: 10 * MiB,      // per optional document
    maxDocuments: 30,
    maxEntries: 200000,
    maxCentralDirectoryBytes: 64 * MiB,
  };
  const TEXT_SUFFIXES = ['.xml', '.html', '.htm', '.qti'];
  const DOCUMENT_SUFFIXES = ['.pdf', '.docx', '.pptx', '.txt', '.md', '.csv'];
  const MIME = {
    mp4: 'video/mp4', m4v: 'video/mp4', mov: 'video/quicktime', webm: 'video/webm',
    mp3: 'audio/mpeg', m4a: 'audio/mp4', wav: 'audio/wav', ogg: 'audio/ogg',
    png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
    webp: 'image/webp', svg: 'image/svg+xml', pdf: 'application/pdf',
    doc: 'application/msword', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    ppt: 'application/vnd.ms-powerpoint', pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    xls: 'application/vnd.ms-excel', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    txt: 'text/plain', csv: 'text/csv', md: 'text/markdown', zip: 'application/zip',
  };
  const FILEBASE = /(?:\$|%24)IMS(?:-|_)CC(?:-|_)FILEBASE(?:\$|%24)\/([^"'?#\s>]+)/gi;

  class SlimExportError extends Error {
    constructor(message) { super(message); this.name = 'SlimExportError'; }
  }

  // ── helpers ──────────────────────────────────────────────────────────────

  function lower(name) { return name.toLowerCase(); }
  function suffix(name) {
    const base = name.slice(name.lastIndexOf('/') + 1);
    const dot = base.lastIndexOf('.');
    return dot < 0 ? '' : lower(base.slice(dot));
  }
  function mimeFor(name) { return MIME[suffix(name).slice(1)] || ''; }
  function isText(name) {
    return name === 'imsmanifest.xml' || name.startsWith('course_settings/') ||
      TEXT_SUFFIXES.includes(suffix(name));
  }
  function checkAbort(signal) {
    if (signal && signal.aborted) throw new SlimExportError('Slimming was cancelled.');
  }
  async function readRange(file, start, end) {
    return new Uint8Array(await file.slice(start, end).arrayBuffer());
  }
  function u16(view, at) { return view.getUint16(at, true); }
  function u32(view, at) { return view.getUint32(at, true); }
  function u64(view, at) {
    const value = view.getUint32(at, true) + view.getUint32(at + 4, true) * 0x100000000;
    if (!Number.isSafeInteger(value)) throw new SlimExportError('Archive offset is too large.');
    return value;
  }
  function safePath(name) {
    if (!name || name.includes('\\') || name.includes('\0') || name.startsWith('/')) return false;
    return !name.replace(/\/$/, '').split('/').some(p => p === '' || p === '.' || p === '..');
  }
  const utf8 = new TextDecoder('utf-8', {fatal: false});
  const latin1 = new TextDecoder('latin1');

  let CRC_TABLE = null;
  function crc32(bytes) {
    if (!CRC_TABLE) {
      CRC_TABLE = new Uint32Array(256);
      for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
        CRC_TABLE[n] = c >>> 0;
      }
    }
    let crc = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
    return (crc ^ 0xFFFFFFFF) >>> 0;
  }

  async function streamBytes(stream, limit) {
    const reader = stream.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      total += value.length;
      if (total > limit) { await reader.cancel(); throw new SlimExportError('An archive entry expands beyond its size limit.'); }
      chunks.push(value);
    }
    const out = new Uint8Array(total);
    let at = 0;
    for (const chunk of chunks) { out.set(chunk, at); at += chunk.length; }
    return out;
  }

  // ── reading ──────────────────────────────────────────────────────────────

  async function readCentralDirectory(file) {
    const size = file.size;
    if (size < 22) throw new SlimExportError('This file is not a Canvas course export (.imscc ZIP).');
    // The end record is normally exactly the last 22 bytes (no archive comment).
    // Read only that first; scan for a comment only if needed, so media near the
    // end of the archive is never touched.
    let eocdAt = -1, view = null;
    for (const tailLength of [Math.min(size, 22), Math.min(size, 22 + 65535)]) {
      const tail = await readRange(file, size - tailLength, size);
      const tv = new DataView(tail.buffer);
      for (let i = tail.length - 22; i >= 0; i--) {
        if (u32(tv, i) === 0x06054b50 && i + 22 + u16(tv, i + 20) === tail.length) {
          eocdAt = size - tailLength + i;
          view = new DataView(tail.buffer, i, 22);
          break;
        }
      }
      if (eocdAt >= 0 || tailLength === size) break;
    }
    if (eocdAt < 0) throw new SlimExportError('This file is not a Canvas course export (.imscc ZIP).');
    let count = u16(view, 10);
    let cdSize = u32(view, 12);
    let cdOffset = u32(view, 16);
    if (count === 0xFFFF || cdSize === 0xFFFFFFFF || cdOffset === 0xFFFFFFFF) {
      // ZIP64: a 20-byte locator sits just before the classic end record.
      const locatorBytes = eocdAt >= 20 ? await readRange(file, eocdAt - 20, eocdAt) : new Uint8Array(0);
      const lv = new DataView(locatorBytes.buffer);
      if (locatorBytes.length < 20 || u32(lv, 0) !== 0x07064b50) throw new SlimExportError('ZIP64 archive is malformed.');
      const recordOffset = u64(lv, 8);
      const record = await readRange(file, recordOffset, recordOffset + 56);
      const rv = new DataView(record.buffer);
      if (record.length < 56 || u32(rv, 0) !== 0x06064b50) throw new SlimExportError('ZIP64 archive is malformed.');
      count = u64(rv, 32);
      cdSize = u64(rv, 40);
      cdOffset = u64(rv, 48);
    }
    if (count > LIMITS.maxEntries) throw new SlimExportError('The archive has too many entries.');
    if (cdSize > LIMITS.maxCentralDirectoryBytes || cdOffset + cdSize > size) {
      throw new SlimExportError('The archive table of contents is malformed or too large.');
    }
    const cd = await readRange(file, cdOffset, cdOffset + cdSize);
    const cv = new DataView(cd.buffer);
    const entries = [];
    let at = 0;
    for (let n = 0; n < count; n++) {
      if (at + 46 > cd.length || u32(cv, at) !== 0x02014b50) throw new SlimExportError('The archive table of contents is malformed.');
      const flags = u16(cv, at + 8);
      const method = u16(cv, at + 10);
      const crc = u32(cv, at + 16);
      let compressed = u32(cv, at + 20);
      let uncompressed = u32(cv, at + 24);
      const nameLength = u16(cv, at + 28);
      const extraLength = u16(cv, at + 30);
      const commentLength = u16(cv, at + 32);
      const externalAttrs = u32(cv, at + 38);
      let localOffset = u32(cv, at + 42);
      const nameBytes = cd.subarray(at + 46, at + 46 + nameLength);
      const rawName = (flags & 0x800 ? utf8 : (/[\x80-\xff]/.test(latin1.decode(nameBytes)) ? utf8 : latin1)).decode(nameBytes);
      // Archives made on Windows may use backslashes; normalize before the safety check.
      const name = rawName.replace(/\\/g, '/').replace(/\/{2,}/g, '/');
      let extra = at + 46 + nameLength;
      const extraEnd = extra + extraLength;
      while (extra + 4 <= extraEnd) {
        const id = u16(cv, extra);
        const length = u16(cv, extra + 2);
        if (id === 0x0001) {
          let field = extra + 4;
          if (uncompressed === 0xFFFFFFFF) { uncompressed = u64(cv, field); field += 8; }
          if (compressed === 0xFFFFFFFF) { compressed = u64(cv, field); field += 8; }
          if (localOffset === 0xFFFFFFFF) { localOffset = u64(cv, field); field += 8; }
        }
        extra += 4 + length;
      }
      at = extraEnd + commentLength;
      const isDir = name.endsWith('/');
      const isLink = ((externalAttrs >>> 16) & 0xF000) === 0xA000;
      if (!safePath(name) || isLink) throw new SlimExportError('The archive contains an unsafe path.');
      // macOS metadata (__MACOSX/, ._files, .DS_Store) is never course content.
      const base = name.slice(name.lastIndexOf('/') + 1);
      if (isDir || name.startsWith('__MACOSX/') || base === '.DS_Store' || base.startsWith('._')) continue;
      entries.push({name, flags, method, crc, compressed, uncompressed, localOffset});
    }
    if (!entries.some(e => e.name === 'imsmanifest.xml')) {
      // A course re-zipped inside one folder: treat that folder as the root.
      const manifests = entries.filter(e => /^[^/]+\/imsmanifest\.xml$/.test(e.name));
      const prefix = manifests.length === 1 ? manifests[0].name.slice(0, -'imsmanifest.xml'.length) : null;
      if (!prefix || entries.some(e => !e.name.startsWith(prefix))) {
        throw new SlimExportError('This ZIP is not a Canvas course export: imsmanifest.xml is missing.');
      }
      for (const entry of entries) entry.name = entry.name.slice(prefix.length);
    }
    return entries;
  }

  async function readEntry(file, entry, limit) {
    if (entry.flags & 1) throw new SlimExportError(`Encrypted entries are not supported: ${entry.name}`);
    if (entry.uncompressed > limit) throw new SlimExportError(`Entry too large: ${entry.name}`);
    const header = await readRange(file, entry.localOffset, entry.localOffset + 30);
    const hv = new DataView(header.buffer);
    if (header.length < 30 || u32(hv, 0) !== 0x04034b50) throw new SlimExportError(`Archive entry is malformed: ${entry.name}`);
    const dataStart = entry.localOffset + 30 + u16(hv, 26) + u16(hv, 28);
    const raw = file.slice(dataStart, dataStart + entry.compressed);
    let data;
    if (entry.method === 0) {
      data = new Uint8Array(await raw.arrayBuffer());
    } else if (entry.method === 8) {
      data = await streamBytes(raw.stream().pipeThrough(new DecompressionStream('deflate-raw')), limit);
    } else {
      throw new SlimExportError(`Unsupported compression in ${entry.name}.`);
    }
    if (data.length !== entry.uncompressed || crc32(data) !== entry.crc) {
      throw new SlimExportError(`Archive entry failed its integrity check: ${entry.name}`);
    }
    return data;
  }

  function referencedPaths(html) {
    const paths = new Set();
    for (const match of html.matchAll(FILEBASE)) {
      try { paths.add('web_resources/' + decodeURIComponent(match[1])); }
      catch (_) { paths.add('web_resources/' + match[1]); }
    }
    return paths;
  }

  /**
   * Read the table of contents and the text entries. Media are never read.
   * Returns {kept, inventory, documents, warnings, stats}.
   */
  async function scanExport(file, options = {}) {
    const {onProgress, signal} = options;
    checkAbort(signal);
    const entries = await readCentralDirectory(file);
    const kept = [];
    const inventory = [];
    const warnings = [];
    let keptBytes = 0;
    let skippedBytes = 0;
    const textEntries = entries.filter(e => isText(e.name));
    let done = 0;
    for (const entry of entries) {
      if (isText(entry.name)) {
        checkAbort(signal);
        if (entry.uncompressed > LIMITS.maxEntryBytes) {
          warnings.push(`Skipped an oversized text entry: ${entry.name}`);
          inventory.push({path: entry.name, bytes: entry.uncompressed, mime: mimeFor(entry.name)});
          skippedBytes += entry.uncompressed;
        } else {
          if (keptBytes + entry.uncompressed > LIMITS.maxKeptBytes) {
            throw new SlimExportError('This course has more than 32 MiB of text; contact support.');
          }
          kept.push({name: entry.name, data: await readEntry(file, entry, LIMITS.maxEntryBytes)});
          keptBytes += entry.uncompressed;
        }
        done++;
        if (onProgress) onProgress({phase: 'reading', done, total: textEntries.length, name: entry.name});
      } else {
        inventory.push({path: entry.name, bytes: entry.uncompressed, mime: mimeFor(entry.name)});
        skippedBytes += entry.uncompressed;
      }
    }
    const referenced = new Set();
    for (const {name, data} of kept) {
      if (['.html', '.htm', '.xml'].includes(suffix(name))) {
        for (const path of referencedPaths(utf8.decode(data))) referenced.add(path);
      }
    }
    const byName = new Map(entries.map(e => [e.name, e]));
    const documents = inventory
      .filter(m => DOCUMENT_SUFFIXES.includes(suffix(m.path)) && referenced.has(m.path)
        && m.bytes <= LIMITS.maxDocumentBytes)
      .map(m => ({path: m.path, name: m.path.slice(m.path.lastIndexOf('/') + 1), bytes: m.bytes}));
    return {
      kept, inventory, documents, warnings, entries: byName,
      stats: {entries: entries.length, keptEntries: kept.length, keptBytes, skippedBytes, archiveBytes: file.size},
    };
  }

  // ── writing ──────────────────────────────────────────────────────────────

  async function deflate(bytes) {
    if (typeof CompressionStream === 'undefined') return null;
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream('deflate-raw'));
    return streamBytes(stream, Infinity);
  }

  /** Write a ZIP (no ZIP64; slim packages are small). Returns a Blob. */
  async function writeZip(files) {
    const encoder = new TextEncoder();
    const parts = [];
    const central = [];
    let offset = 0;
    for (const file of files) {
      const name = encoder.encode(file.name);
      const data = file.data;
      const crc = crc32(data);
      const packed = await deflate(data);
      const method = packed && packed.length < data.length ? 8 : 0;
      const body = method === 8 ? packed : data;
      const local = new DataView(new ArrayBuffer(30));
      local.setUint32(0, 0x04034b50, true);
      local.setUint16(4, 20, true);
      local.setUint16(6, 0x800, true);
      local.setUint16(8, method, true);
      local.setUint32(14, crc, true);
      local.setUint32(18, body.length, true);
      local.setUint32(22, data.length, true);
      local.setUint16(26, name.length, true);
      parts.push(new Uint8Array(local.buffer), name, body);
      const entry = new DataView(new ArrayBuffer(46));
      entry.setUint32(0, 0x02014b50, true);
      entry.setUint16(4, 20, true);
      entry.setUint16(6, 20, true);
      entry.setUint16(8, 0x800, true);
      entry.setUint16(10, method, true);
      entry.setUint32(16, crc, true);
      entry.setUint32(20, body.length, true);
      entry.setUint32(24, data.length, true);
      entry.setUint16(28, name.length, true);
      entry.setUint32(42, offset, true);
      central.push(new Uint8Array(entry.buffer), name);
      offset += 30 + name.length + body.length;
      if (offset > 0xFFFFFFF0) throw new SlimExportError('The slim package is unexpectedly large.');
    }
    const cdSize = central.reduce((sum, part) => sum + part.length, 0);
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, files.length, true);
    end.setUint16(10, files.length, true);
    end.setUint32(12, cdSize, true);
    end.setUint32(16, offset, true);
    return new Blob([...parts, ...central, new Uint8Array(end.buffer)], {type: 'application/zip'});
  }

  /**
   * Build the slim package: kept text entries, media_inventory.json and the
   * documents the instructor ticked (under materials/).
   */
  async function buildSlimPackage(file, scan, options = {}) {
    const {includeDocuments = [], onProgress, signal} = options;
    const offered = new Set(scan.documents.map(d => d.path));
    if (includeDocuments.length > LIMITS.maxDocuments) {
      throw new SlimExportError(`Choose at most ${LIMITS.maxDocuments} documents.`);
    }
    const files = scan.kept.map(k => ({name: k.name, data: k.data}));
    const usedNames = new Set();
    let index = 0;
    for (const path of includeDocuments) {
      checkAbort(signal);
      if (!offered.has(path)) throw new SlimExportError('A selected document was not offered for inclusion.');
      const entry = scan.entries.get(path);
      let base = path.slice(path.lastIndexOf('/') + 1).replace(/[^\w.\- ]+/g, '_') || 'document';
      while (usedNames.has(lower(base))) base = `${++index}-${base}`;
      usedNames.add(lower(base));
      files.push({name: 'materials/' + base, data: await readEntry(file, entry, LIMITS.maxDocumentBytes)});
      if (onProgress) onProgress({phase: 'documents', name: base});
    }
    const inventory = scan.inventory.filter(m => !includeDocuments.includes(m.path));
    files.push({name: 'media_inventory.json', data: new TextEncoder().encode(JSON.stringify(inventory))});
    if (onProgress) onProgress({phase: 'packaging'});
    const blob = await writeZip(files);
    return {
      blob,
      summary: {
        archiveBytes: scan.stats.archiveBytes, slimBytes: blob.size,
        textEntries: scan.kept.length, mediaEntries: inventory.length,
        mediaBytesSkipped: inventory.reduce((sum, m) => sum + m.bytes, 0),
        documentsIncluded: includeDocuments.length, warnings: scan.warnings,
      },
    };
  }

  const api = {scanExport, buildSlimPackage, writeZip, crc32, SlimExportError, LIMITS};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SlimExport = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
