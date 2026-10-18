'use strict';
const path = require('node:path');
const root = path.resolve(__dirname, '..');
require('esbuild').buildSync({
  absWorkingDir: root,
  entryPoints: ['web/hosted-upload.js'],
  outfile: 'python/app/static/hosted-upload.js',
  bundle: true, platform: 'browser', format: 'iife',
  target: ['chrome109', 'firefox115', 'safari16'],
  minify: true, sourcemap: false,
});
