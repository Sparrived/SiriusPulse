import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/pages/orchestration.js', 'utf8');

for (const removedTask of ['diary_generate', 'diary_consolidate', 'topic_cluster']) {
  assert.equal(
    source.includes(`key: '${removedTask}'`),
    false,
    `${removedTask} should not be configurable on the orchestration page`,
  );
}

assert.equal(source.includes("key: 'memory_extract'"), true);
