#!/usr/bin/env node
'use strict';

// Validate real Canvas exports end to end, without uploading anything:
// browser slimming (the same code the page runs) -> server import -> privacy
// strip -> course map. Writes a Markdown report.
//
//   node scripts/validate_exports.cjs <folder of .imscc/.zip> [--report report.md]
//
// Set PYTHON to the interpreter with the repository's requirements installed.
// Exports are read locally; course content is not copied into the report.

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const SlimExport = require('../python/app/static/slim-export.js');

const ROOT = path.resolve(__dirname, '..');
const PYTHON = process.env.PYTHON || 'python3';
const args = process.argv.slice(2);
const folder = args[0];
const reportPath = args.includes('--report') ? args[args.indexOf('--report') + 1] : null;
if (!folder || !fs.statSync(folder).isDirectory()) {
  console.error('Usage: node scripts/validate_exports.cjs <folder> [--report report.md]');
  process.exit(2);
}

const IMPORT = String.raw`
import json, sys
sys.path[:0] = [sys.argv[1] + "/python"]
from slim_import import import_slim
from course_map import build_course_map, compact_map
from privacy_strip import strip_course
results = {}
for name, path in json.loads(sys.stdin.read()).items():
    try:
        model = import_slim(path)
        model, privacy = strip_course(model)
        course_map = build_course_map(model)
        c = model["provenance"]["counts"]
        results[name] = {"ok": True, "counts": c, "warnings": len(model["provenance"]["warnings"]),
                         "warning_kinds": sorted({w.split(":")[0][:60] for w in model["provenance"]["warnings"]}),
                         "questions": sum(len(q["questions"]) for q in model["quizzes"].values()),
                         "bank_questions": sum(len(b["questions"]) for b in model["question_banks"]),
                         "graded": course_map["assessment_summary"]["graded_items"],
                         "weighted": course_map["grading"]["weighted_groups"],
                         "syllabus": bool(model["course"]["syllabus_html"]),
                         "privacy_removed": privacy["total"], "map_chars": len(compact_map(course_map))}
    except Exception as exc:
        results[name] = {"ok": False, "stage": "import", "error": f"{type(exc).__name__}: {exc}"[:300]}
print(json.dumps(results))
`;

(async () => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-validate-'));
  const rows = {};
  const slimPaths = {};
  for (const name of fs.readdirSync(folder).filter(n => /\.(imscc|zip)$/i.test(n)).sort()) {
    const started = Date.now();
    try {
      const file = await fs.openAsBlob(path.join(folder, name));
      const scan = await SlimExport.scanExport(file);
      const { blob, summary } = await SlimExport.buildSlimPackage(file, scan, {
        includeDocuments: scan.documents.map(d => d.path).slice(0, 5) });
      const slimPath = path.join(work, name + '.slim.zip');
      fs.writeFileSync(slimPath, Buffer.from(await blob.arrayBuffer()));
      slimPaths[name] = slimPath;
      rows[name] = { slim: summary, slimMs: Date.now() - started };
    } catch (error) {
      rows[name] = { error: { stage: 'slimming', error: error.message } };
    }
  }
  const imported = JSON.parse(execFileSync(PYTHON, ['-c', IMPORT, ROOT], {
    input: JSON.stringify(slimPaths), encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }));
  const mb = n => (n / 1024 / 1024).toFixed(n < 1024 * 1024 ? 3 : 1);
  const lines = ['# Canvas export validation', '',
    `${Object.keys(rows).length} files from \`${path.basename(folder)}\`, ${new Date().toISOString()}`, '',
    '| Export | Result | Export MB | Slim MB | Pages | Assign. | Quizzes (questions) | Banks (questions) | Discussions | Modules | Rubrics | Outcomes | Weighted | Syllabus | Media | Student details removed | Warnings |',
    '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|'];
  let failures = 0;
  for (const [name, row] of Object.entries(rows)) {
    const result = row.error || (imported[name] && !imported[name].ok ? imported[name] : null);
    if (result) {
      failures++;
      lines.push(`| ${name} | ❌ ${result.stage}: ${String(result.error).replace(/\|/g, '/')} |` + ' |'.repeat(15));
      continue;
    }
    const r = imported[name];
    const c = r.counts;
    lines.push(`| ${name} | ✅ | ${mb(row.slim.archiveBytes)} | ${mb(row.slim.slimBytes)} | ${c.pages} | ${c.assignments} | `
      + `${c.quizzes} (${r.questions}) | ${c.question_banks} (${r.bank_questions}) | ${c.discussions} | ${c.modules} | ${c.rubrics} | `
      + `${c.outcomes} | ${r.weighted ? 'yes' : 'no'} | ${r.syllabus ? 'yes' : 'no'} | ${c.media} | ${r.privacy_removed} | `
      + `${r.warnings}${r.warning_kinds.length ? ': ' + r.warning_kinds.join('; ').replace(/\|/g, '/') : ''} |`);
  }
  lines.push('', `${Object.keys(rows).length - failures} imported, ${failures} failed.`);
  const report = lines.join('\n') + '\n';
  if (reportPath) fs.writeFileSync(reportPath, report); else process.stdout.write(report);
  fs.rmSync(work, { recursive: true, force: true });
  process.exit(failures && !reportPath ? 1 : 0);
})();
