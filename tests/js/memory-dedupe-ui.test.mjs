import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const source = readFileSync('sirius_pulse/webui/static/pages/memory-viz.js', 'utf8');
for (const value of ['memoryDedupeBtn', '清理重复', '/persona/memory-units/dedupe/scan', '/persona/memory-units/dedupe/status', '/persona/memory-units/dedupe/apply', 'queued', 'ready', 'applying', 'completed', 'stale', 'failed', 'clearInterval(dedupePollTimer)']) assert.ok(source.includes(value));
