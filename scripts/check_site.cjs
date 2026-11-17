#!/usr/bin/env node
/*
 * Open a built HAX course site in a real browser and check what a student would get.
 *
 *   node scripts/check_site.cjs <site folder> [--pages N] [--shots <folder>]
 *
 * <site folder> is an unpacked "Download course site" ZIP (site.json, pages/, files/), such as the
 * site/ folder scripts/evaluate_consultant.py writes. The site is served from a local port and
 * rendered with HAX's own components from HAX's CDN, exactly as a static host would.
 *
 * For each page it checks, in the rendered page (through shadow roots):
 *   - every HAX element on the page is a registered, upgraded component
 *   - every question has at least one answer marked correct, and at least two options
 *   - every picture loaded
 *   - every link to another course page goes to a page that exists
 *   - nothing only Canvas could resolve is left in the page
 * and reports page errors. Prints a JSON summary; exits 1 if anything failed.
 *
 * Needs Playwright with Chromium. It is not a dependency of this project: install it anywhere
 * (npm i playwright) and run with NODE_PATH=<that folder>/node_modules.
 */
'use strict';

const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

let playwright;
try { playwright = require('playwright'); } catch (_) {
  console.error('Playwright is not installed. Run "npm i playwright" in a scratch folder and set NODE_PATH to its node_modules.');
  process.exit(2);
}

const ELEMENTS = ['multiple-choice', 'true-false-question', 'self-check', 'stop-note', 'a11y-collapse', 'oer-schema', 'video-player'];
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp' };

function serve(root) {
  return new Promise(resolve => {
    const server = http.createServer((request, response) => {
      let file = path.join(root, decodeURIComponent(request.url.split('?')[0]));
      if (!file.startsWith(root)) { response.writeHead(403); return response.end(); }
      // HAX routes pages by their address, so anything that is not a file is the site shell.
      if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(root, 'index.html');
      response.writeHead(200, { 'content-type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream' });
      response.end(fs.readFileSync(file));
    }).listen(0, '127.0.0.1', () => resolve(server));
  });
}

// Runs in the page. Looks through shadow roots, because HAX renders page content inside them.
function inspect([elements, slugs]) {
  const all = selector => {
    const found = [];
    const walk = node => {
      if (!node.querySelectorAll) return;
      found.push(...node.querySelectorAll(selector));
      node.querySelectorAll('*').forEach(child => child.shadowRoot && walk(child.shadowRoot));
    };
    walk(document);
    return found;
  };
  const problems = [];
  const counts = {};
  for (const tag of elements) {
    const nodes = all(tag);
    counts[tag] = nodes.length;
    if (nodes.length && !customElements.get(tag)) problems.push(`${tag} is on the page but HAX did not load it`);
  }
  for (const question of [...all('multiple-choice'), ...all('true-false-question')]) {
    const answers = question.answers || [];
    const label = (question.question || '').slice(0, 70);
    if (answers.length < 2) problems.push(`question has ${answers.length} option(s): ${label}`);
    else if (!answers.some(answer => answer.correct)) problems.push(`question has no answer marked correct: ${label}`);
  }
  // Pictures and links are read through shadow roots too: HAX renders the page content inside them.
  const local = all('img').filter(image => /^files\//.test(image.getAttribute('src') || ''));
  for (const image of local) {
    if (!(image.complete && image.naturalWidth > 0)) problems.push(`picture did not load: ${image.getAttribute('src')}`);
  }
  const links = [...new Set(all('a[href]').map(a => a.getAttribute('href')))];
  const canvasOnly = /IMS-CC-FILEBASE|WIKI_REFERENCE|CANVAS_[A-Z_]+REFERENCE|IMSCC_ITEM_REFERENCE/;
  for (const href of links) {
    if (canvasOnly.test(href)) { problems.push(`link only Canvas could follow: ${href.slice(0, 80)}`); continue; }
    if (/^(https?:|mailto:|tel:|#|files\/|javascript:)/.test(href)) continue;
    const slug = decodeURIComponent(href.split('#')[0].split('?')[0]).replace(/^\.?\//, '').replace(/\/$/, '');
    if (slug && !slugs.includes(slug) && !/^x\//.test(slug)) problems.push(`link to a page that does not exist: ${href.slice(0, 80)}`);
  }
  for (const image of all('img')) {
    if (canvasOnly.test(image.getAttribute('src') || '')) problems.push(`picture only Canvas could show: ${image.getAttribute('src').slice(0, 80)}`);
  }
  return { counts, pictures: local.length, links: links.length, problems };
}

async function main() {
  const args = process.argv.slice(2);
  const root = path.resolve(args[0] || '');
  if (!args[0] || !fs.existsSync(path.join(root, 'site.json'))) {
    console.error('usage: node scripts/check_site.cjs <site folder> [--pages N] [--shots <folder>]');
    process.exit(2);
  }
  const option = name => (args.includes(name) ? args[args.indexOf(name) + 1] : null);
  const limit = Number(option('--pages')) || Infinity;
  const shots = option('--shots');
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'site.json'), 'utf8'));
  const slugs = manifest.items.map(item => item.slug);
  // Check the pages with the most to go wrong first: those that hold HAX elements or pictures.
  const weight = item => {
    const html = fs.readFileSync(path.join(root, item.location), 'utf8');
    return (html.match(/<(multiple-choice|true-false-question|self-check|video-player)/g) || []).length * 2
      + (html.match(/src="files\//g) || []).length;
  };
  const chosen = manifest.items.map(item => ({ item, weight: weight(item) }))
    .sort((a, b) => b.weight - a.weight).slice(0, limit).map(row => row.item);

  const server = await serve(root);
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await playwright.chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 1400 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message.slice(0, 200)));
  const results = [];
  const totals = { pages: 0, pictures: 0, questions: 0, problems: 0 };
  for (const item of chosen) {
    const expected = fs.readFileSync(path.join(root, item.location), 'utf8');
    const wanted = ELEMENTS.filter(tag => expected.includes('<' + tag));
    await page.goto(`${origin}/${item.slug}`, { waitUntil: 'domcontentloaded' });
    try {
      // Ready when the page's own elements are registered and its pictures have settled.
      await page.waitForFunction(tags => tags.every(tag => customElements.get(tag))
        && document.querySelector('haxcms-site-builder'), wanted, { timeout: 45000 });
      await page.waitForTimeout(wanted.length || /src="files\//.test(expected) ? 2500 : 800);
    } catch (_) { /* reported below as elements that did not load */ }
    const result = await page.evaluate(inspect, [ELEMENTS, slugs])
      .catch(error => ({ counts: {}, pictures: 0, links: 0, problems: ['could not inspect the page: ' + error.message] }));
    for (const tag of wanted) {
      const authored = (expected.match(new RegExp('<' + tag + '[ >]', 'g')) || []).length;
      if ((result.counts[tag] || 0) < authored) result.problems.push(`${authored} ${tag} written, ${result.counts[tag] || 0} shown`);
    }
    totals.pages++;
    totals.pictures += result.pictures;
    totals.questions += (result.counts['multiple-choice'] || 0) + (result.counts['true-false-question'] || 0);
    totals.problems += result.problems.length;
    if (result.problems.length) results.push({ page: item.title, address: item.slug, problems: result.problems });
    if (shots && results.length + totals.pages <= 6) {
      await page.screenshot({ path: path.join(shots, `${String(totals.pages).padStart(2, '0')}-${item.slug.slice(0, 40)}.png`) });
    }
  }
  await browser.close();
  server.close();
  console.log(JSON.stringify({ site: manifest.title, pagesInSite: manifest.items.length, ...totals,
    pageErrors: [...new Set(errors)].slice(0, 10), failing: results.slice(0, 40) }, null, 1));
  process.exit(totals.problems ? 1 : 0);
}

main().catch(error => { console.error(error); process.exit(1); });
