#!/usr/bin/env node
'use strict';

// Package the browser extension for one Course Consultant deployment.
//   node scripts/build-extension.cjs --origin https://consultant.example.edu
// Writes dist/extension/ (load unpacked) and dist/course-consultant-extension.zip
// (Chrome Web Store upload). The development build in extension/ targets
// http://127.0.0.1:5050.

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const originArg = args[args.indexOf('--origin') + 1];
if (!args.includes('--origin') || !originArg) {
  console.error('Usage: node scripts/build-extension.cjs --origin https://your-course-consultant-site');
  process.exit(2);
}
const origin = new URL(originArg).origin;
if (!/^https:\/\//.test(origin) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin)) {
  console.error('The origin must use HTTPS (or be localhost for development).');
  process.exit(2);
}

const out = path.join(ROOT, 'dist', 'extension');
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });
for (const name of ['background.js', 'collect.js', 'popup.html', 'popup.js', 'popup.css', 'PRIVACY.md']) {
  fs.copyFileSync(path.join(ROOT, 'extension', name), path.join(out, name));
}
const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'extension', 'manifest.json'), 'utf8'));
manifest.externally_connectable = { matches: [`${new URL(origin).protocol}//${new URL(origin).hostname}/*`] };
fs.writeFileSync(path.join(out, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
fs.writeFileSync(path.join(out, 'config.js'), `self.COURSE_CONSULTANT_ORIGIN = ${JSON.stringify(origin)};\n`);
// The automated-test hook is not shipped.
const background = fs.readFileSync(path.join(out, 'background.js'), 'utf8');
const hook = background.indexOf('// Test hook:');
if (hook >= 0) fs.writeFileSync(path.join(out, 'background.js'), background.slice(0, hook));

const zip = path.join(ROOT, 'dist', 'course-consultant-extension.zip');
fs.rmSync(zip, { force: true });
execFileSync('zip', ['-qr', zip, '.'], { cwd: out });
console.log(`Built for ${origin}:\n  ${out}\n  ${zip}`);
