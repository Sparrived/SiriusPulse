import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const appSource = readFileSync('sirius_pulse/webui/static/app.js', 'utf8');
const memorySource = readFileSync('sirius_pulse/webui/static/pages/memory-viz.js', 'utf8');

assert.match(appSource, /'memory-viz': \{ title: '记忆管理'/);
assert.match(appSource, /\{ page: 'memory-viz', icon: '◲', label: '记忆管理' \}/);
assert.doesNotMatch(appSource, /memory-dashboard|page: 'diary'|page: 'glossary'/);

for (const oldPage of [
  'sirius_pulse/webui/static/pages/memory-dashboard.js',
  'sirius_pulse/webui/static/pages/memory-dashboard.html',
  'sirius_pulse/webui/static/pages/diary.js',
  'sirius_pulse/webui/static/pages/diary.html',
  'sirius_pulse/webui/static/pages/glossary.js',
  'sirius_pulse/webui/static/pages/glossary.html',
  'sirius_pulse/webui/static/pages/memory-nav.js',
]) {
  assert.equal(existsSync(oldPage), false, `${oldPage} should be removed`);
}

for (const apiPath of [
  '/persona/diary',
  '/persona/glossary',
  '/persona/users',
  '/persona/conversations',
]) {
  assert.match(memorySource, new RegExp(apiPath.replaceAll('/', '\\/')));
}

assert.match(memorySource, /记忆管理工作台/);
assert.match(memorySource, /openEditor\(state\.tab, null\)/);
assert.match(memorySource, /deleteItem\(state\.tab, filtered\[Number\(btn\.dataset\.delete\)\]\)/);
