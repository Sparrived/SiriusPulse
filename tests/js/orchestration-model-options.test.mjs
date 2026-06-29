import assert from 'node:assert/strict';
import { resolveCompositeModelValue } from '../../sirius_pulse/webui/static/pages/orchestration-model-options.js';

const configuredOptions = [
  { value: 'deepseek/deepseek-v4-flash', label: 'deepseek/deepseek-v4-flash', tags: [] },
];
const discoveredOptions = [
  { value: 'aliyun-bailian/deepseek-v4-flash', label: 'aliyun-bailian/deepseek-v4-flash', tags: [] },
];

assert.equal(
  resolveCompositeModelValue('deepseek-v4-flash', [...discoveredOptions], configuredOptions),
  'deepseek/deepseek-v4-flash',
  'configured provider choice should win over discovered catalog entries',
);

assert.equal(
  resolveCompositeModelValue('deepseek-v4-flash', [...discoveredOptions], []),
  'deepseek-v4-flash',
  'bare saved values should not be rebound to a provider from discovered catalog only',
);

assert.equal(
  resolveCompositeModelValue('aliyun-bailian/deepseek-v4-flash', [...discoveredOptions], []),
  'aliyun-bailian/deepseek-v4-flash',
  'explicit provider-prefixed values should remain selectable from discovered options',
);
