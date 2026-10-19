#!/usr/bin/env node
'use strict';

const { spawnSync, spawn } = require('child_process');
const fs   = require('fs');
const path = require('path');
const os   = require('os');
const crypto = require('crypto');

// ── constants ─────────────────────────────────────────────────────────────────

const PKG         = require('../package.json');
const PKG_PYTHON  = path.join(__dirname, '..', 'python');   // bundled source
let DATA_DIR    = path.join(os.homedir(), '.canvas-course-builder');
let VENV_DIR    = path.join(DATA_DIR, '.venv');
let VERSION_FILE = path.join(DATA_DIR, '.version');
const HAX_VERSION = '26.8.1';

// Paths that belong to user data — never overwritten during an update
const USER_DATA_PATTERNS = ['.env', 'exports', 'hax_prep', 'mentor_data', 'local_imports', 'sites', 'imports', 'builds',
  '__pycache__', '.venv', '.version', '.pip_installed'];

function bundledEntry(name) {
  return !USER_DATA_PATTERNS.includes(name) &&
    !/\.(?:pyc|db|sqlite3?|haxcourse|imscc|zip)(?:-|$)/i.test(name) &&
    !/^course_.*_ai_.*\.html$/i.test(name) &&
    !name.startsWith('.');
}

function sourceFingerprint(directory) {
  const hash = crypto.createHash('sha256');
  function visit(base, prefix = '') {
    for (const entry of fs.readdirSync(base, {withFileTypes: true}).sort((a, b) => a.name.localeCompare(b.name))) {
      if (!bundledEntry(entry.name) || entry.isSymbolicLink()) continue;
      const file = path.join(base, entry.name);
      if (entry.isDirectory()) visit(file, prefix + entry.name + '/');
      else { hash.update(prefix + entry.name); hash.update(fs.readFileSync(file)); }
    }
  }
  visit(directory);
  return hash.digest('hex');
}

// ── helpers ───────────────────────────────────────────────────────────────────

function log(msg)  { process.stdout.write(`\x1b[36m[hax-ai-canvas]\x1b[0m ${msg}\n`); }
function warn(msg) { process.stdout.write(`\x1b[33m[hax-ai-canvas]\x1b[0m ${msg}\n`); }
function die(msg)  { process.stderr.write(`\x1b[31m[hax-ai-canvas] ERROR:\x1b[0m ${msg}\n`); process.exit(1); }

function run(cmd, args = [], opts = {}) {
  const result = spawnSync(cmd, args, { encoding: 'utf8', ...opts, shell: false });
  if (result.error && result.error.code !== 'ENOENT') throw result.error;
  return result;
}
function parseArgs(args) {
  const result = {dataDir: DATA_DIR, course: null, prepare: null};
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === '--prepare') {
      result.prepare = args.slice(i + 1);
      break; // The preparer owns all remaining options, including --output.
    }
    if (!['--data-dir', '--course'].includes(arg)) throw new Error(`Unknown option: ${arg}`);
    if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error(`Missing value for ${arg}`);
    result[arg === '--data-dir' ? 'dataDir' : 'course'] = path.resolve(args[++i]);
  }
  if (result.prepare && result.course) throw new Error('--course cannot be combined with --prepare.');
  return result;
}

function configureDataDir(directory) {
  DATA_DIR = path.resolve(directory);
  VENV_DIR = path.join(DATA_DIR, '.venv');
  VERSION_FILE = path.join(DATA_DIR, '.version');
}

function checkNode(version = process.versions.node) {
  if (Number(version.split('.')[0]) < 22) throw new Error('Node.js 22 or newer is required. Install Node 22+ and retry.');
}

function resolveHaxCli(searchPaths = [__dirname]) {
  // Resolve relative to this installed package, not the caller's working directory.
  // Some package exports hide package.json, so walk up from its public entry too.
  let manifest;
  try {
    manifest = require.resolve('@haxtheweb/create/package.json', {paths: searchPaths});
  } catch (_) {
    let directory;
    try { directory = path.dirname(require.resolve('@haxtheweb/create', {paths: searchPaths})); }
    catch (_) { throw new Error('Managed HAX CLI is missing. Install project dependencies (npm install), or reinstall hax-ai-canvas.'); }
    while (directory !== path.dirname(directory)) {
      const candidate = path.join(directory, 'package.json');
      if (fs.existsSync(candidate) && JSON.parse(fs.readFileSync(candidate, 'utf8')).name === '@haxtheweb/create') {
        manifest = candidate;
        break;
      }
      directory = path.dirname(directory);
    }
  }
  if (!manifest) throw new Error('Cannot resolve the managed official HAX CLI.');
  const pkg = JSON.parse(fs.readFileSync(manifest, 'utf8'));
  if (pkg.name !== '@haxtheweb/create' || pkg.version !== HAX_VERSION) throw new Error(`Managed HAX CLI must be @haxtheweb/create ${HAX_VERSION}. Reinstall dependencies.`);
  const bin = typeof pkg.bin === 'string' ? pkg.bin : (pkg.bin?.hax || pkg.bin?.create);
  if (!bin) throw new Error('Managed HAX CLI package has no supported executable.');
  const cli = path.resolve(path.dirname(manifest), bin);
  if (!cli.startsWith(path.dirname(manifest) + path.sep) || !fs.statSync(cli).isFile()) throw new Error('Invalid managed HAX CLI executable.');
  return cli;
}

function localEnvironment(haxCli, course = null) {
  const env = {...process.env, HAX_EXECUTION_MODE: 'local', HAX_DATA_DIR: DATA_DIR,
    HAX_NODE: process.execPath, HAX_HAX_CLI: haxCli};
  delete env.HAX_STARTUP_COURSE;
  if (course) env.HAX_STARTUP_COURSE = course;
  return env;
}

function managedChild(command, args, options) {
  const child = spawn(command, args, {...options, detached: process.platform !== 'win32', shell: false});
  const handlers = {};
  let timer;
  function signalGroup(signal) {
    if (!child.pid) return;
    try {
      if (process.platform === 'win32') child.kill(signal);
      else process.kill(-child.pid, signal);
    } catch (error) { if (error.code !== 'ESRCH') warn(error.message); }
  }
  for (const signal of ['SIGINT', 'SIGTERM']) {
    handlers[signal] = () => {
      signalGroup(signal);
      // Give Flask/managed site children time to stop, then reap the whole group.
      if (!timer) timer = setTimeout(() => {
        if (process.platform === 'win32') {
          spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], {shell: false, stdio: 'ignore'});
        } else signalGroup('SIGKILL');
      }, 8000).unref();
    };
    process.on(signal, handlers[signal]);
  }
  child.on('error', error => die(`Cannot launch local runtime: ${error.message}`));
  child.on('exit', (code, signal) => {
    if (timer) clearTimeout(timer);
    // Reap children even if Python exits before its own cleanup completes.
    if (process.platform !== 'win32') {
      signalGroup('SIGTERM');
      try {
        process.kill(-child.pid, 0);
        // A surviving child must not outlive a completed launcher.
        setTimeout(() => signalGroup('SIGKILL'), 1000);
      } catch (error) { if (error.code !== 'ESRCH') warn(error.message); }
    }
    for (const name of Object.keys(handlers)) process.removeListener(name, handlers[name]);
    process.exitCode = code ?? (signal === 'SIGINT' ? 130 : signal ? 143 : 0);
  });
  return child;
}

// ── step 1: find Python 3.10+ ────────────────────────────────────────────────

function pythonSupported(text) {
  const match = text.match(/Python (\d+)\.(\d+)/);
  return !!match && Number(match[1]) === 3 && Number(match[2]) >= 10;
}
function findPython() {
  for (const candidate of ['python3', 'python']) {
    const r = run(candidate, ['--version']);
    if (r.status !== 0) continue;
    const version = (r.stdout || '') + (r.stderr || '');
    if (pythonSupported(version)) {
      log(`Found ${candidate} (${version.trim()})`);
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
  if (fs.existsSync(dest) && fs.lstatSync(dest).isSymbolicLink()) throw new Error('Refusing to update a symbolic-link destination.');
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (!bundledEntry(entry.name) || entry.isSymbolicLink()) continue;
    const srcPath  = path.join(src,  entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      if (fs.existsSync(destPath) && fs.lstatSync(destPath).isSymbolicLink()) throw new Error('Refusing to update a symbolic-link destination.');
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function syncPythonFiles() {
  if (fs.existsSync(DATA_DIR) && fs.lstatSync(DATA_DIR).isSymbolicLink()) throw new Error('The local data directory cannot be a symbolic link.');
  if (fs.existsSync(VERSION_FILE) && fs.lstatSync(VERSION_FILE).isSymbolicLink()) throw new Error('The version marker cannot be a symbolic link.');
  const installedVersion = fs.existsSync(VERSION_FILE)
    ? fs.readFileSync(VERSION_FILE, 'utf8').trim()
    : null;

  const signature = `${PKG.version}:${sourceFingerprint(PKG_PYTHON)}`;
  if (installedVersion === signature) {
    log(`Python files up to date (v${PKG.version})`);
    return false; // no change
  }

  const verb = installedVersion ? `Updating bundled code to v${PKG.version}` : `Installing v${PKG.version}`;
  log(`${verb} — copying files to ${DATA_DIR}`);

  fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });

  // Walk bundled python/ and copy, skipping user data paths at the top level
  for (const entry of fs.readdirSync(PKG_PYTHON, { withFileTypes: true })) {
    if (!bundledEntry(entry.name) || entry.isSymbolicLink()) continue; // preserve user data
    const src  = path.join(PKG_PYTHON, entry.name);
    const dest = path.join(DATA_DIR,   entry.name);
    if (entry.isDirectory()) {
      copyDir(src, dest);
    } else {
      if (fs.existsSync(dest) && fs.lstatSync(dest).isSymbolicLink()) throw new Error('Refusing to update a symbolic-link destination.');
      fs.copyFileSync(src, dest);
    }
  }

  fs.writeFileSync(VERSION_FILE, signature, 'utf8');
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
    const version = run(venvPython, ['--version']);
    if (version.status !== 0 || !pythonSupported((version.stdout || '') + (version.stderr || ''))) {
      die('The managed virtual environment requires Python 3.10+. Choose a new --data-dir to create a compatible isolated runtime.');
    }
    log('Virtual environment already exists.');
    // Still check pip exists (might have been a broken venv from before)
    if (!fs.existsSync(venvPip)) {
      _ensurePipInVenv(venvPython, venvPip);
    }
    return venvPython;
  }

  // Try creating venv normally first
  log('Creating Python virtual environment…');
  const packageMarker = path.join(DATA_DIR, '.pip_installed');
  if (fs.existsSync(packageMarker)) fs.unlinkSync(packageMarker);
  let r = run(python, ['-m', 'venv', VENV_DIR], { stdio: 'inherit' });

  // On Debian/Ubuntu, venv module may not be installed. Try --without-pip as fallback.
  if (r.status !== 0) {
    warn('venv failed — retrying with --without-pip…');
    r = run(python, ['-m', 'venv', '--without-pip', VENV_DIR], { stdio: 'inherit' });
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
  run(venvPython, ['-m', 'ensurepip', '--upgrade'], { stdio: 'inherit' });
  if (fs.existsSync(venvPip)) { log('pip installed via ensurepip.'); return; }

  // Attempt 2: download get-pip.py (works everywhere, no sudo needed)
  log('ensurepip unavailable — downloading get-pip.py…');
  const getPipPath = path.join(DATA_DIR, 'get-pip.py');
  const dl = run(venvPython, ['-c',
    'import sys, urllib.request; urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", sys.argv[1])',
    getPipPath]);
  if (dl.status === 0 && fs.existsSync(getPipPath)) {
    log('Installing pip via get-pip.py…');
    const gp = run(venvPython, [getPipPath], { stdio: 'inherit' });
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

  const requirementsHash = crypto.createHash('sha256').update(fs.readFileSync(reqFile)).digest('hex');
  if (!versionChanged && fs.existsSync(markerFile) &&
      fs.readFileSync(markerFile, 'utf8').trim() === requirementsHash) {
    log('Python packages already installed.');
    return;
  }

  log('Installing Python packages (this takes a minute on first run)…');
  const r = run(pip, ['install', '-r', reqFile], { stdio: 'inherit' });
  if (r.status !== 0) die('pip install failed. Check the output above.');

  fs.writeFileSync(markerFile, requirementsHash, 'utf8');
  log('Packages installed.');
}

// ── step 5: launch Flask ──────────────────────────────────────────────────────

function launchApp(venvPython, environment) {
  const appScript = path.join(DATA_DIR, 'app', 'app.py');

  if (!fs.existsSync(appScript)) {
    die(`app.py not found at ${appScript} — installation may be incomplete.`);
  }

  console.log('');
  console.log('  ╔══════════════════════════════════════════╗');
  console.log('  ║       HAX AI Canvas Course Builder       ║');
  console.log('  ║   Starting your local Faculty Mentor.    ║');
  console.log('  ║   The app opens its browser when ready. ║');
  console.log('  ║   Press Ctrl+C to stop.                  ║');
  console.log('  ╚══════════════════════════════════════════╝');
  console.log('');

  return managedChild(venvPython, [appScript], {
    stdio: 'inherit',
    cwd: DATA_DIR,          // pipeline writes exports/, *.db etc. relative to cwd
    env: environment,
  });
}

// ── main ──────────────────────────────────────────────────────────────────────

function main() {
  console.log(`\nhax-ai-canvas v${PKG.version}\n`);
  const args = parseArgs(process.argv.slice(2));
  checkNode();
  configureDataDir(args.dataDir);
  if (args.course) {
    const info = fs.lstatSync(args.course);
    if (!info.isFile() || info.isSymbolicLink() || !/\.(imscc|haxcourse)$/i.test(args.course)) {
      throw new Error('--course requires a regular .imscc or .haxcourse file.');
    }
  }
  const haxCli = args.prepare ? '' : resolveHaxCli();
  const environment = localEnvironment(haxCli, args.course);

  const python        = findPython();
  const versionChanged = syncPythonFiles();
  const venvPython    = ensureVenv(python, versionChanged);
  ensurePackages(venvPython, versionChanged);
  if (args.prepare) {
    managedChild(venvPython, [path.join(DATA_DIR, 'prepare_course.py'), ...args.prepare], {
      stdio: 'inherit', env: environment,
    });
  } else {
    launchApp(venvPython, environment);
  }
}
if (require.main === module) {
  try { main(); } catch (error) { die(error.message); }
}
module.exports = {sourceFingerprint, bundledEntry, parseArgs, checkNode, resolveHaxCli,
  localEnvironment, configureDataDir, syncPythonFiles, run, managedChild, pythonSupported};
