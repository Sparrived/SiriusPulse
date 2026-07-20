import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

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

for (const helper of ['getToolCalls', 'renderChainToolCallMessage', 'renderChainToolResultMessage']) {
  assert.equal(
    source.includes(`function ${helper}`),
    true,
    `conversation chains should define ${helper} for tool activity rendering`,
  );
}

assert.equal(
  source.includes("role === 'tool'"),
  true,
  'tool result messages should use a dedicated rendering branch',
);

assert.equal(
  source.includes('工具调用结果'),
  true,
  'tool result cards should be distinguishable in the basic-memory view',
);

const context = {
  createScopedPage: () => ({ $: () => null, $$: () => [], use: () => {}, isActive: () => true }),
  createRealtimeRefresh: () => ({ stop: () => {} }),
  document: {
    createElement: () => ({
      set textContent(value) { this.innerHTML = String(value); },
      innerHTML: '',
    }),
  },
};
context.globalThis = context;
vm.runInNewContext(
  source
    .replace(/^import .*;$/gm, '')
    .replace(/^export /gm, '')
    + '\nglobalThis.renderChainMessagesForTest = renderChainMessages;',
  context,
);

const renderedToolChain = context.renderChainMessagesForTest([
  {
    role: 'assistant',
    tool_calls: [{
      id: 'call_lookup',
      type: 'function',
      function: { name: 'lookup_weather', arguments: '{"city":"Shanghai"}' },
    }],
  },
  { role: 'tool', tool_call_id: 'call_lookup', content: '{"temperature":30}' },
]);
assert.match(renderedToolChain, /工具调用/);
assert.match(renderedToolChain, /lookup_weather/);
assert.match(renderedToolChain, /Shanghai/);
assert.match(renderedToolChain, /工具调用结果 · lookup_weather/);
assert.match(renderedToolChain, /temperature/);
