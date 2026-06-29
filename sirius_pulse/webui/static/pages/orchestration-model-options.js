export function buildLegacyModelChoice(model) {
  if (typeof model === 'object') return model;
  return { value: model, label: model, tags: [] };
}

export function toModelSelectOptions(modelChoices) {
  return (modelChoices || []).map(model => {
    const value = typeof model === 'object' ? model.value : model;
    const label = typeof model === 'object' ? model.label : model;
    const tags = (typeof model === 'object' && Array.isArray(model.tags)) ? model.tags : [];
    return { value, label, tags };
  });
}

export function stripProviderPrefix(value) {
  if (!value) return '';
  const idx = value.indexOf('/');
  return idx >= 0 ? value.substring(idx + 1) : value;
}

export function resolveCompositeModelValue(value, options, configuredOptions = options) {
  if (!value) return '';

  const exact = options.find(option => option.value === value);
  if (exact) return exact.value;

  const configuredSuffix = configuredOptions.find(option => option.value.endsWith('/' + value));
  return configuredSuffix ? configuredSuffix.value : value;
}
