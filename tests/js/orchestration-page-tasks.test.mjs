import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/pages/orchestration.js', 'utf8');

assert.match(
  source,
  /let\s+autoSave\s*[;=]/,
  'orchestration page should declare module-scoped autoSave before init assigns it',
);

assert.equal(source.includes("key: 'memory_extract'"), true);

for (const removedTask of ['diary_generate', 'diary_consolidate', 'topic_cluster']) {
  assert.equal(
    source.includes(removedTask),
    false,
    `${removedTask} was removed from the task registry and must not appear on the orchestration page`,
  );
}

for (const discoveredCatalogHook of ['loadModelsDevForTypes', 'buildModelChoicesByType']) {
  assert.equal(
    source.includes(discoveredCatalogHook),
    false,
    `orchestration model dropdowns should only use configured provider models, not ${discoveredCatalogHook}`,
  );
}

assert.equal(
  source.includes('stripProviderPrefix'),
  false,
  'orchestration saves should preserve provider/model values to avoid same-model-name ambiguity',
);
