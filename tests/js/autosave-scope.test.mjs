import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const pagesDir = 'sirius_pulse/webui/static/pages';
const offenders = [];

for (const file of readdirSync(pagesDir).filter((name) => name.endsWith('.js')).sort()) {
  const source = readFileSync(join(pagesDir, file), 'utf8');
  if (!source.includes('autoSave')) continue;

  const declaredModuleAutoSave = /(?:let|const|var)\s+autoSave\b/.test(source);
  const lines = source.split('\n');

  lines.forEach((line, index) => {
    if (!/\bfunction\s+\w+\s*\([^)]*\)\s*\{/.test(line)) return;
    if (!line.includes('autoSave')) return;
    if (/\bautoSave\b/.test(line.match(/\(([^)]*)\)/)?.[1] || '')) return;
    if (declaredModuleAutoSave) return;
    offenders.push(`${file}:${index + 1}: function uses autoSave without receiving or declaring it`);
  });
}

assert.deepEqual(offenders, []);