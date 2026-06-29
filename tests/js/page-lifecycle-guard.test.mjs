import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const pagesDir = 'sirius_pulse/webui/static/pages';
const offenders = [];

for (const file of readdirSync(pagesDir).filter((name) => name.endsWith('.js')).sort()) {
  if (file === 'globe-renderer.js' || file === 'orchestration-model-options.js' || file === 'realtime.js' || file === 'memory-nav.js') continue;
  const source = readFileSync(join(pagesDir, file), 'utf8');
  if (!/export\s+(async\s+)?function\s+init/.test(source)) continue;
  if (/import\s+\{[^}]*\$[^}]*\}\s+from\s+['"]\.\.\/components\.js['"]/.test(source)) {
    offenders.push(`${file}: imports global $`);
  }
  if (/document\.(getElementById|querySelector|querySelectorAll)\s*\(/.test(source)) {
    offenders.push(`${file}: uses global document query`);
  }
  if (/\bset(Interval|Timeout)\s*\(/.test(source)) {
    offenders.push(`${file}: uses unmanaged timer`);
  }
  if (/(window|document)\.addEventListener\s*\(/.test(source)) {
    offenders.push(`${file}: uses unmanaged global listener`);
  }
  const nakedListeners = [...source.matchAll(/(?<!ctx\.on\([^\n]*)\.addEventListener\(/g)].length;
  if (nakedListeners > 0 && !/params\?\.ctx|\{\s*ctx\s*\}/.test(source)) {
    offenders.push(`${file}: has naked addEventListener without ctx`);
  }
}

assert.deepEqual(offenders, []);
