#!/usr/bin/env node
/*
 * Slim a Canvas export from the command line, with the same code the browser runs
 * (python/app/static/slim-export.js). Used by scripts/evaluate_consultant.py so real,
 * multi-gigabyte exports can be tested without a browser.
 *
 *   node scripts/slim_export.cjs <export.imscc> <slim.zip> [--images] [--documents]
 *
 * --images     include the pictures the pages show (what the instructor's tick-box does)
 * --documents  include every offered document as text
 * Prints a one-line JSON summary.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const SlimExport = require(path.join(__dirname, '..', 'python', 'app', 'static', 'slim-export.js'));

async function main() {
  const [source, target, ...flags] = process.argv.slice(2);
  if (!source || !target) {
    console.error('usage: node scripts/slim_export.cjs <export.imscc> <slim.zip> [--images] [--documents]');
    process.exit(2);
  }
  const file = await fs.openAsBlob(source);
  const scan = await SlimExport.scanExport(file);
  const { blob, summary } = await SlimExport.buildSlimPackage(file, scan, {
    includeImages: flags.includes('--images'),
    includeDocuments: flags.includes('--documents') ? scan.documents.map(d => d.path) : [],
  });
  fs.writeFileSync(target, Buffer.from(await blob.arrayBuffer()));
  console.log(JSON.stringify({
    ...summary, warnings: summary.warnings.slice(0, 20),
    imagesOffered: scan.images.length, imageBytesOffered: scan.imageBytes, imagesTooLarge: scan.imagesLeftOut,
    documentsOffered: scan.documents.length,
  }));
}

main().catch(error => {
  console.error(error.name === 'SlimExportError' ? error.message : error);
  process.exit(1);
});
