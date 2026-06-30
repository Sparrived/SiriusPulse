import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const roots = ['sirius_pulse/webui/static'];
const allowed = new Set([
  'sirius_pulse/webui/static/api.js',
  'sirius_pulse/webui/static/app.js',
  'sirius_pulse/webui/static/page-context.js',
]);
const offenders = [];

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const file = join(dir, name).replaceAll('\\', '/');
    if (statSync(file).isDirectory()) walk(file);
    else if (file.endsWith('.js') && !allowed.has(file)) {
      const source = readFileSync(file, 'utf8');
      if (/\bfetch\s*\(/.test(source)) offenders.push(file);
    }
  }
}

for (const root of roots) walk(root);
assert.deepEqual(offenders, []);
