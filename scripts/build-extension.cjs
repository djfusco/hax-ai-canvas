#!/usr/bin/env node
'use strict';

// Package the browser extension for one Course Consultant deployment.
//   node scripts/build-extension.cjs --origin https://consultant.example.edu [--browser firefox]
//   [--handoff bridge] [--out dir]
// Chrome/Edge (default): writes dist/extension/ and dist/course-consultant-extension.zip.
// Firefox: dist/extension-firefox/ and dist/course-consultant-extension-firefox.zip.
// Firefox has no externally_connectable, so its build hands the course to the
// site through bridge.js, a content script limited to the site's import page.
// --handoff bridge uses the bridge in a Chrome build (used by the tests).

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
const option = name => (args.includes(name) ? args[args.indexOf(name) + 1] : null);
const browser = option('--browser') || 'chrome';
if (!['chrome', 'firefox'].includes(browser)) {
  console.error('--browser must be chrome or firefox.');
  process.exit(2);
}
const handoff = browser === 'firefox' ? 'bridge' : (option('--handoff') || 'external');
const origin = new URL(originArg).origin;
if (!/^https:\/\//.test(origin) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin)) {
  console.error('The origin must use HTTPS (or be localhost for development).');
  process.exit(2);
}

const out = path.resolve(option('--out') || path.join(ROOT, 'dist', browser === 'firefox' ? 'extension-firefox' : 'extension'));
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });
for (const name of ['background.js', 'collect.js', 'bridge.js', 'popup.html', 'popup.js', 'popup.css', 'PRIVACY.md']) {
  fs.copyFileSync(path.join(ROOT, 'extension', name), path.join(out, name));
}
const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'extension', 'manifest.json'), 'utf8'));
const sitePattern = `${new URL(origin).protocol}//${new URL(origin).hostname}`;
if (handoff === 'bridge') {
  delete manifest.externally_connectable;
  // The bridge runs only on the import page; bridge.js also checks the exact origin.
  manifest.content_scripts = [{ matches: [`${sitePattern}/mentor/import/extension*`],
    js: ['config.js', 'bridge.js'], run_at: 'document_start' }];
} else {
  manifest.externally_connectable = { matches: [`${sitePattern}/*`] };
}
if (browser === 'firefox') {
  manifest.background = { scripts: ['config.js', 'collect.js', 'background.js'] };
  manifest.browser_specific_settings = { gecko: {
    id: `course-consultant@${new URL(origin).hostname}`, strict_min_version: '121.0' } };
  delete manifest.minimum_chrome_version;
}
fs.writeFileSync(path.join(out, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
fs.writeFileSync(path.join(out, 'config.js'), `self.COURSE_CONSULTANT_ORIGIN = ${JSON.stringify(origin)};\n`
  + `self.COURSE_CONSULTANT_HANDOFF = ${JSON.stringify(handoff)};\n`);
// The automated-test hook is not shipped.
const background = fs.readFileSync(path.join(out, 'background.js'), 'utf8');
const hook = background.indexOf('// Test hook:');
if (hook >= 0 && !args.includes('--keep-test-hook')) fs.writeFileSync(path.join(out, 'background.js'), background.slice(0, hook));

const zip = path.join(path.dirname(out), `course-consultant-extension${browser === 'firefox' ? '-firefox' : ''}.zip`);
fs.rmSync(zip, { force: true });
execFileSync('zip', ['-qr', zip, '.'], { cwd: out });
console.log(`Built for ${origin}:\n  ${out}\n  ${zip}`);
