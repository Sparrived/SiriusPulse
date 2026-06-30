import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/pages/conversation-history.js', 'utf8');

for (const selector of ['.conversation-delete', '.chain-toggle', '.chain-detail']) {
  assert.equal(
    source.includes(`scopedPage.$('${selector}')`),
    false,
    `${selector} must use scopedPage.$$ because it is a CSS selector list`,
  );
  assert.equal(
    source.includes(`scopedPage.$$('${selector}')`),
    true,
    `${selector} should be queried as a scoped selector list`,
  );
}

assert.equal(
  source.includes("root.querySelectorAll('.chain-section-header')"),
  true,
  'chain section headers should be bound inside the lazily rendered chain detail',
);

assert.equal(
  source.includes('function renderInjectedToolTags'),
  true,
  'conversation history should render injected tool names as tags',
);

assert.equal(
  source.includes('m.injected_tool_names'),
  true,
  'assistant messages should read injected_tool_names from conversation data',
);

assert.equal(
  source.includes('点击后加载消息链详情'),
  true,
  'conversation chains should render lazily instead of prebuilding hidden details',
);

assert.equal(
  source.includes('renderChainDetailForButton'),
  true,
  'conversation chain details should be rendered on demand when expanded',
);
