#!/usr/bin/env node
'use strict';

const { execSync, spawnSync, spawn } = require('child_process');
const fs   = require('fs');
const path = require('path');
const os   = require('os');

// ── constants ─────────────────────────────────────────────────────────────────

const PKG         = require('../package.json');
const PKG_PYTHON  = path.join(__dirname, '..', 'python');   // bundled source
const DATA_DIR    = path.join(os.homedir(), '.canvas-course-builder');
const VENV_DIR    = path.join(DATA_DIR, '.venv');
const VERSION_FILE = path.join(DATA_DIR, '.version');
const PORT        = 5050;

// Paths that belong to user data — never overwritten during an update
const USER_DATA_PATTERNS = ['.env', 'exports', 'hax_prep', '.venv', '.version', '.pip_installed'];

// ── helpers ───────────────────────────────────────────────────────────────────

function log(msg)  { process.stdout.write(`\x1b[36m[hax-ai-canvas]\x1b[0m ${msg}\n`); }
function warn(msg) { process.stdout.write(`\x1b[33m[hax-ai-canvas]\x1b[0m ${msg}\n`); }
function die(msg)  { process.stderr.write(`\x1b[31m[hax-ai-canvas] ERROR:\x1b[0m ${msg}\n`); process.exit(1); }

function run(cmd, opts = {}) {
  const result = spawnSync(cmd, { shell: true, encoding: 'utf8', ...opts });
  if (result.error) throw result.error;
  return result;
}

// ── step 1: find Python 3.10+ ────────────────────────────────────────────────

function findPython() {
  for (const candidate of ['python3', 'python']) {
    const r = run(`${candidate} --version 2>&1`);
    if (r.status !== 0) continue;
    const match = (r.stdout || '').match(/Python (\d+)\.(\d+)/);
    if (!match) continue;
    const [, major, minor] = match.map(Number);
    if (major === 3 && minor >= 10) {
      log(`Found ${candidate} (${r.stdout.trim()})`);
      return candidate;
    }
  }
  die(
    'Python 3.10 or newer is required but was not found.\n' +
    '  Download from: https://www.python.org/downloads/\n' +
    '  Then re-run:   npx hax-ai-canvas'
  );
}

// ── step 2: copy Python files to DATA_DIR ────────────────────────────────────

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath  = path.join(src,  entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function syncPythonFiles() {
  const installedVersion = fs.existsSync(VERSION_FILE)
    ? fs.readFileSync(VERSION_FILE, 'utf8').trim()
    : null;

  if (installedVersion === PKG.version) {
    log(`Python files up to date (v${PKG.version})`);
    return false; // no change
  }

  const verb = installedVersion ? `Updating v${installedVersion} → v${PKG.version}` : `Installing v${PKG.version}`;
  log(`${verb} — copying files to ${DATA_DIR}`);

  fs.mkdirSync(DATA_DIR, { recursive: true });

  // Walk bundled python/ and copy, skipping user data paths at the top level
  for (const entry of fs.readdirSync(PKG_PYTHON, { withFileTypes: true })) {
    if (USER_DATA_PATTERNS.includes(entry.name)) continue; // preserve user data
    const src  = path.join(PKG_PYTHON, entry.name);
    const dest = path.join(DATA_DIR,   entry.name);
    if (entry.isDirectory()) {
      copyDir(src, dest);
    } else {
      fs.copyFileSync(src, dest);
    }
  }

  fs.writeFileSync(VERSION_FILE, PKG.version, 'utf8');
  log('Files copied.');
  return true; // version changed
}

// ── step 3: create venv ───────────────────────────────────────────────────────

function ensureVenv(python, versionChanged) {
  const isWindows = process.platform === 'win32';
  const venvPython = isWindows
    ? path.join(VENV_DIR, 'Scripts', 'python.exe')
    : path.join(VENV_DIR, 'bin', 'python');
  const venvPip = isWindows
    ? path.join(VENV_DIR, 'Scripts', 'pip.exe')
    : path.join(VENV_DIR, 'bin', 'pip');

  if (!versionChanged && fs.existsSync(venvPython)) {
    log('Virtual environment already exists.');
    // Still check pip exists (might have been a broken venv from before)
    if (!fs.existsSync(venvPip)) {
      _ensurePipInVenv(venvPython, venvPip);
    }
    return venvPython;
  }

  // Try creating venv normally first
  log('Creating Python virtual environment…');
  let r = run(`${python} -m venv "${VENV_DIR}"`, { stdio: 'inherit' });

  // On Debian/Ubuntu, venv module may not be installed. Try --without-pip as fallback.
  if (r.status !== 0) {
    warn('venv failed — retrying with --without-pip…');
    r = run(`${python} -m venv --without-pip "${VENV_DIR}"`, { stdio: 'inherit' });
    if (r.status !== 0) die(
      'Failed to create virtual environment.\n' +
      '  On Ubuntu/Debian, you may need to install:\n' +
      '    sudo apt install python3-venv\n' +
      '  Then re-run: npx hax-ai-canvas'
    );
  }

  // Make sure pip is available in the venv
  _ensurePipInVenv(venvPython, venvPip);

  log('Virtual environment created.');
  return venvPython;
}

function _ensurePipInVenv(venvPython, venvPip) {
  if (fs.existsSync(venvPip)) return;

  // Attempt 1: ensurepip (works on most systems)
  log('pip not found in venv — trying ensurepip…');
  run(`"${venvPython}" -m ensurepip --upgrade`, { stdio: 'inherit' });
  if (fs.existsSync(venvPip)) { log('pip installed via ensurepip.'); return; }

  // Attempt 2: download get-pip.py (works everywhere, no sudo needed)
  log('ensurepip unavailable — downloading get-pip.py…');
  const getPipPath = path.join(DATA_DIR, 'get-pip.py');
  const dl = run(
    `"${venvPython}" -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '${getPipPath.replace(/'/g, "'\\''")}')"`
  );
  if (dl.status === 0 && fs.existsSync(getPipPath)) {
    log('Installing pip via get-pip.py…');
    const gp = run(`"${venvPython}" "${getPipPath}"`, { stdio: 'inherit' });
    // Clean up
    try { fs.unlinkSync(getPipPath); } catch (_) {}
    if (gp.status === 0 && fs.existsSync(venvPip)) { log('pip installed via get-pip.py.'); return; }
  }

  die(
    'Could not install pip in the virtual environment.\n' +
    '  On Ubuntu/Debian, run:\n' +
    '    sudo apt install python3-venv python3-pip\n' +
    '  Then re-run: npx hax-ai-canvas'
  );
}

// ── step 4: install requirements ─────────────────────────────────────────────

function ensurePackages(venvPython, versionChanged) {
  const isWindows  = process.platform === 'win32';
  const pip        = isWindows
    ? path.join(VENV_DIR, 'Scripts', 'pip.exe')
    : path.join(VENV_DIR, 'bin', 'pip');
  const markerFile = path.join(DATA_DIR, '.pip_installed');
  const reqFile    = path.join(DATA_DIR, 'requirements.txt');

  if (!versionChanged && fs.existsSync(markerFile)) {
    log('Python packages already installed.');
    return;
  }

  log('Installing Python packages (this takes a minute on first run)…');
  const r = run(`"${pip}" install -r "${reqFile}"`, { stdio: 'inherit' });
  if (r.status !== 0) die('pip install failed. Check the output above.');

  fs.writeFileSync(markerFile, new Date().toISOString(), 'utf8');
  log('Packages installed.');
}

// ── step 5: launch Flask ──────────────────────────────────────────────────────

function launchApp(venvPython) {
  const appScript = path.join(DATA_DIR, 'app', 'app.py');

  if (!fs.existsSync(appScript)) {
    die(`app.py not found at ${appScript} — installation may be incomplete.`);
  }

  console.log('');
  console.log('  ╔══════════════════════════════════════════╗');
  console.log('  ║       HAX AI Canvas Course Builder       ║');
  console.log(`  ║   Opening browser at http://127.0.0.1:${PORT}  ║`);
  console.log('  ║   Press Ctrl+C to stop.                  ║');
  console.log('  ╚══════════════════════════════════════════╝');
  console.log('');

  const child = spawn(venvPython, [appScript], {
    stdio: 'inherit',
    cwd: DATA_DIR,          // pipeline writes exports/, *.db etc. relative to cwd
    env: { ...process.env },
  });

  // Forward Ctrl+C cleanly
  process.on('SIGINT', () => {
    child.kill('SIGINT');
  });
  process.on('SIGTERM', () => {
    child.kill('SIGTERM');
  });

  child.on('exit', (code) => {
    process.exit(code ?? 0);
  });
}

// ── main ──────────────────────────────────────────────────────────────────────

(function main() {
  console.log(`\nhax-ai-canvas v${PKG.version}\n`);

  const python        = findPython();
  const versionChanged = syncPythonFiles();
  const venvPython    = ensureVenv(python, versionChanged);
  ensurePackages(venvPython, versionChanged);
  launchApp(venvPython);
})();
