import { get } from "../app.js";

const modelDevCache = {};
const CAPABILITY_LABELS = {
  tool_call: '函数调用',
  reasoning: '推理',
  structured_output: '结构化',
  vision: '视觉',
  audio: '音频',
};

const CAPABILITY_CLASSES = {
  函数调用: 'tool',
  推理: 'reason',
  结构化: 'tag',
  视觉: 'vision',
  音频: 'audio',
};

export async function loadModelsDevForType(providerType, { refresh = false } = {}) {
  if (!refresh && modelDevCache[providerType]) return modelDevCache[providerType];
  try {
    const data = await get(`/providers/models-dev/${encodeURIComponent(providerType)}`);
    const models = Array.isArray(data.models) ? data.models : [];
    modelDevCache[providerType] = models;
    return models;
  } catch (error) {
    console.warn('[models-dev] 加载失败:', providerType, error);
    modelDevCache[providerType] = [];
    return [];
  }
}

export async function loadModelsDevForTypes(providerTypes, options = {}) {
  const uniqueTypes = [...new Set((providerTypes || []).filter(Boolean))];
  const results = await Promise.all(
    uniqueTypes.map(async providerType => [providerType, await loadModelsDevForType(providerType, options)])
  );
  return Object.fromEntries(results);
}

export function getModelDevCache(providerType) {
  return modelDevCache[providerType] || [];
}

export function getCapabilityTags(model) {
  return Object.entries(CAPABILITY_LABELS)
    .filter(([key]) => model?.[key])
    .map(([, label]) => label);
}

export function getCapabilityClass(label) {
  return CAPABILITY_CLASSES[label] || 'tag';
}

export function buildModelChoice(providerType, model) {
  const modelId = model.id || model.model_id || model.name || model;
  return {
    value: `${providerType}/${modelId}`,
    label: `${providerType}/${modelId}`,
    tags: getCapabilityTags(model),
  };
}

export function buildModelChoicesByType(modelsByType) {
  return Object.entries(modelsByType).flatMap(([providerType, models]) =>
    models.map(model => buildModelChoice(providerType, model))
  );
}

export function buildModelOptions(models) {
  return (models || []).map(model => ({
    value: model,
    label: model,
    tags: getCapabilityTags(model),
  }));
}

export function buildDevModelCard(model, added = false) {
  const badges = getCapabilityTags(model)
    .map(tag => `<span class="cap-tag cap-${getCapabilityClass(tag)}">${tag}</span>`)
    .join('');
  const ctxLabel = model.context > 0
    ? (model.context >= 1000000 ? `${(model.context / 1000000).toFixed(0)}M tokens` : `${Math.round(model.context / 1000)}K tokens`)
    : '';
  const costLabel = model.input_cost > 0
    ? `¥${(model.input_cost * 7.25).toFixed(1)}/¥${(model.output_cost * 7.25).toFixed(1)}`
    : '';

  return `<div class="model-card${added ? ' model-card-added' : ''}" data-dev-model="${model.id}" data-cap-tool_call="${!!model.tool_call}" data-cap-reasoning="${!!model.reasoning}" data-cap-vision="${!!model.vision}" data-cap-audio="${!!model.audio}" style="cursor:${added ? 'default' : 'pointer'};opacity:${added ? '0.55' : '1'}">
    <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px" title="${model.name || model.id}">${model.id}</div>
    <div style="display:flex;align-items:center;gap:3px;flex-wrap:wrap;margin-bottom:4px">${badges || '<span style="font-size:10px;color:var(--text-3)">—</span>'}</div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-3)">
      <span>${ctxLabel}</span><span>${costLabel}</span>
    </div>
    ${added ? '<div style="position:absolute;top:4px;right:6px;font-size:10px;color:var(--success)">✓ 已添加</div>' : ''}
  </div>`;
}
