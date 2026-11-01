// 插件 UI 元数据的浏览器端二次校验——逐行移植自旧版 static/plugin-ui-schema.js。
// 服务端已经清洗过一遍；这里在浏览器边界再验一次，任何一处不合规就整体返回 null，
// 退回朴素的分组表单，而不是半信半疑地套用一部分布局（fail closed）。
// 修改规则时请同步旧版文件与后端 _safe_plugin_ui_schema，三处必须一致。
/* eslint-disable @typescript-eslint/no-explicit-any */
const UNSAFE_KEYS = new Set(['__proto__', 'prototype', 'constructor']);
const TOP_KEYS = new Set(['version', 'layout', 'title', 'description', 'sections', 'parameters']);
const SECTION_KEYS = new Set(['id', 'title', 'description', 'parameters', 'columns', 'collapsed', 'tone']);
const BASE_PRESENTATION_KEYS = new Set(['label', 'help', 'span']);
const OBJECT_ARRAY_KEYS = new Set([
  'add_label',
  'item_placeholder',
  'empty_title',
  'empty_description',
  'item_title_field',
  'item_fallback_field',
  'item_subtitle_field',
  'item_badge_field',
  'item_status_field',
  'fields',
  'fieldsets',
]);
const FIELDSET_KEYS = new Set(['id', 'title', 'description', 'fields', 'collapsed']);
const STRING_WIDGETS = new Set(['text', 'url', 'path', 'code', 'textarea']);
const TONES = new Set(['default', 'accent', 'muted']);
const SECRET_NAMES = new Set([
  'password',
  'passwords',
  'secret',
  'secrets',
  'token',
  'tokens',
  'key',
  'keys',
  'api_key',
  'api_keys',
  'access_token',
  'refresh_token',
  'authorization',
  'auth',
  'authentication',
  'bearer',
  'client_secret',
  'credential',
  'credentials',
  'session',
  'session_id',
]);
const SECRET_SUFFIXES = [
  '_token',
  '_tokens',
  '_key',
  '_keys',
  '_secret',
  '_secrets',
  '_password',
  '_passwords',
  '_credential',
  '_credentials',
  '_auth',
  '_session',
];
const MAX_SCHEMA_BYTES = 64 * 1024;
const TEXT_LIMITS: Record<string, number> = Object.freeze({
  title: 120,
  description: 600,
  label: 120,
  help: 600,
  placeholder: 240,
  unit: 32,
  true_label: 64,
  false_label: 64,
  add_label: 120,
  item_placeholder: 160,
  empty_title: 120,
  empty_description: 600,
});
const MAX_PARAMETERS = 128;
const MAX_SECTIONS = 16;
const MAX_DEPTH = 8;
const ID_PATTERN = /^[a-z][a-z0-9_-]{0,31}$/;

function isRecord(value: any): value is Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function normalizeName(value: any): string {
  return String(value ?? '')
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

export function isSecretField(name: any, type: any): boolean {
  const normalized = normalizeName(name);
  return (
    ['password', 'secret'].includes(String(type ?? '').toLowerCase()) ||
    SECRET_NAMES.has(normalized) ||
    SECRET_SUFFIXES.some((suffix) => normalized.endsWith(suffix))
  );
}

function textContainsCredential(value: string): boolean {
  const inlineCredential =
    /(?:authorization\s*:\s*\S+|bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:password|passphrase|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|credential)\s*[=:]\s*\S+)/i;
  if (inlineCredential.test(value)) return true;
  if (!value.includes('://') && !value.startsWith('//') && !/[?#]/.test(value)) return false;
  const starts: number[] = [];
  let offset = 0;
  while ((offset = value.indexOf('://', offset)) !== -1) {
    starts.push(offset + 3);
    offset += 3;
  }
  if (value.startsWith('//')) starts.push(2);
  for (const start of starts) {
    const authority = value.slice(start).split(/[\\/?#]/, 1)[0];
    if (authority.includes('@') && authority.lastIndexOf('@') > 0) return true;
  }
  const suffix = value.includes('?')
    ? value.slice(value.indexOf('?') + 1)
    : value.includes('#')
      ? value.slice(value.indexOf('#') + 1)
      : '';
  for (const component of suffix.split('#')) {
    for (const pair of component.split('&')) {
      const [rawKey, rawValue = ''] = pair.split('=', 2);
      let key = rawKey;
      let itemValue = rawValue;
      try {
        key = decodeURIComponent(rawKey.replace(/\+/g, ' '));
        itemValue = decodeURIComponent(rawValue.replace(/\+/g, ' '));
      } catch {
        // Malformed percent escapes are not interpreted as credentials here;
        // the backend remains the authoritative metadata validator.
      }
      if (isSecretField(key, 'str') && itemValue) return true;
    }
  }
  return false;
}

function safeText(value: any, maxLength: number = TEXT_LIMITS.description): boolean {
  return (
    typeof value === 'string' &&
    value.length <= maxLength &&
    !Array.from(value).some((char) => char.codePointAt(0)! < 32 && char !== '\n' && char !== '\t') &&
    !value.includes('<') &&
    !value.includes('>') &&
    !value.includes('${') &&
    !value.includes('{{') &&
    !value.includes('}}') &&
    !textContainsCredential(value)
  );
}

function hasOwn(value: any, key: string): boolean {
  return value !== null && value !== undefined && Object.prototype.hasOwnProperty.call(value, key);
}

function ownOr(value: any, key: string, fallback: any): any {
  return hasOwn(value, key) ? value[key] : fallback;
}

function hasOnlyKeys(value: any, allowed: Set<string>): boolean {
  return Object.keys(value).every((key) => !UNSAFE_KEYS.has(key) && allowed.has(key));
}

function isSafeJsonTree(raw: any): boolean {
  const stack: [any, number][] = [[raw, 0]];
  const seen = new WeakSet();
  while (stack.length) {
    const [value, depth] = stack.pop()!;
    if (depth > MAX_DEPTH) return false;
    if (value === null || ['string', 'boolean'].includes(typeof value)) continue;
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) return false;
      continue;
    }
    if ((!Array.isArray(value) && !isRecord(value)) || seen.has(value)) return false;
    let prototype = Object.getPrototypeOf(value);
    while (prototype) {
      if (Object.getOwnPropertyDescriptor(prototype, 'toJSON')) return false;
      prototype = Object.getPrototypeOf(prototype);
    }
    if (Object.getOwnPropertySymbols(value).length) return false;
    const descriptors: Record<string, PropertyDescriptor> = Object.getOwnPropertyDescriptors(value);
    if (Object.values(descriptors).some((descriptor) => !('value' in descriptor))) return false;
    seen.add(value);
    if (Array.isArray(value)) {
      const names = Object.getOwnPropertyNames(value);
      if (
        names.length !== value.length + 1 ||
        names.some(
          (name, index) => name !== 'length' && (String(Number(name)) !== name || Number(name) !== index),
        )
      )
        return false;
      for (let index = 0; index < value.length; index += 1) {
        stack.push([descriptors[String(index)].value, depth + 1]);
      }
    } else {
      if (Object.getOwnPropertyNames(value).length !== Object.keys(value).length) return false;
      for (const [key, descriptor] of Object.entries(descriptors)) {
        if (UNSAFE_KEYS.has(key)) return false;
        stack.push([descriptor.value, depth + 1]);
      }
    }
  }
  return true;
}

function collectParameters(parameters: any): Map<string, any> | null {
  if (!Array.isArray(parameters) || parameters.length > MAX_PARAMETERS) return null;
  const result = new Map();
  for (const parameter of parameters) {
    if (!isRecord(parameter) || !hasOwn(parameter, 'name')) return null;
    const name = typeof parameter.name === 'string' ? parameter.name : '';
    if (!name || UNSAFE_KEYS.has(name) || result.has(name)) return null;
    result.set(name, parameter);
  }
  return result;
}

function collectFields(parameter: any): Map<string, any> | null {
  if (
    !hasOwn(parameter, 'fields') ||
    !Array.isArray(parameter.fields) ||
    parameter.fields.length > MAX_PARAMETERS
  )
    return null;
  const result = new Map();
  for (const field of parameter.fields) {
    if (!isRecord(field) || !hasOwn(field, 'name')) return null;
    const name = typeof field.name === 'string' ? field.name : '';
    if (!name || UNSAFE_KEYS.has(name) || result.has(name)) return null;
    result.set(name, field);
  }
  return result;
}

function presentationKeys(type: string, name: string, objectArray: boolean): Set<string> {
  const result = new Set(BASE_PRESENTATION_KEYS);
  if (isSecretField(name, type)) return result;
  if (['str', 'string'].includes(type)) {
    result.add('placeholder');
    result.add('widget');
  } else if (['int', 'float', 'number'].includes(type)) {
    result.add('unit');
  } else if (['bool', 'boolean'].includes(type)) {
    result.add('widget');
    result.add('true_label');
    result.add('false_label');
  } else if (['list', 'array'].includes(type)) {
    result.add('add_label');
    result.add('item_placeholder');
  } else if (type === 'object_array' && objectArray) {
    for (const key of OBJECT_ARRAY_KEYS) result.add(key);
  }
  return result;
}

function validatePresentation(raw: any, type: string, name: string, { objectArray = false } = {}): boolean {
  if (!isRecord(raw) || !hasOnlyKeys(raw, presentationKeys(type, name, objectArray))) return false;
  for (const key of [
    'label',
    'help',
    'placeholder',
    'unit',
    'true_label',
    'false_label',
    'add_label',
    'item_placeholder',
    'empty_title',
    'empty_description',
  ]) {
    if (!hasOwn(raw, key)) continue;
    if (!safeText(raw[key], TEXT_LIMITS[key])) return false;
  }
  if (hasOwn(raw, 'span') && (!Number.isInteger(raw.span) || raw.span < 1 || raw.span > 12)) return false;
  if (hasOwn(raw, 'widget')) {
    const compatible =
      (raw.widget === 'switch' && ['bool', 'boolean'].includes(type)) ||
      (STRING_WIDGETS.has(raw.widget) && ['str', 'string'].includes(type));
    if (!compatible) return false;
  }
  return true;
}

function validateObjectArrayPresentation(raw: any, parameter: any): boolean {
  const fields = collectFields(parameter);
  if (!fields) return false;
  const scalarTypes = new Set(['str', 'string', 'int', 'float', 'number', 'bool', 'boolean']);
  for (const key of [
    'item_title_field',
    'item_fallback_field',
    'item_subtitle_field',
    'item_badge_field',
    'item_status_field',
  ]) {
    if (!hasOwn(raw, key)) continue;
    if (typeof raw[key] !== 'string') return false;
    const field = fields.get(raw[key]);
    if (!field) return false;
    const fieldType = String(ownOr(field, 'type', '')).toLowerCase();
    if (!scalarTypes.has(fieldType) || isSecretField(raw[key], fieldType)) return false;
    if (key === 'item_status_field' && !['bool', 'boolean'].includes(fieldType)) return false;
  }

  const fieldPresentations = ownOr(raw, 'fields', {});
  if (!isRecord(fieldPresentations) || Object.keys(fieldPresentations).length > MAX_PARAMETERS) {
    return false;
  }
  for (const [fieldName, presentation] of Object.entries(fieldPresentations)) {
    const field = fields.get(fieldName);
    if (!field) return false;
    const fieldType = String(ownOr(field, 'type', 'str')).toLowerCase();
    if (!validatePresentation(presentation, fieldType, fieldName)) return false;
  }

  const fieldsets = ownOr(raw, 'fieldsets', []);
  if (!Array.isArray(fieldsets) || fieldsets.length > MAX_SECTIONS) return false;
  const ids = new Set();
  const assigned = new Set();
  for (const fieldset of fieldsets) {
    if (
      !isRecord(fieldset) ||
      !hasOnlyKeys(fieldset, FIELDSET_KEYS) ||
      !hasOwn(fieldset, 'id') ||
      typeof fieldset.id !== 'string' ||
      !ID_PATTERN.test(fieldset.id) ||
      ids.has(fieldset.id) ||
      !hasOwn(fieldset, 'fields') ||
      !Array.isArray(fieldset.fields) ||
      fieldset.fields.some((name: any) => typeof name !== 'string') ||
      typeof ownOr(fieldset, 'collapsed', false) !== 'boolean'
    )
      return false;
    ids.add(fieldset.id);
    if (new Set(fieldset.fields).size !== fieldset.fields.length) return false;
    for (const fieldName of fieldset.fields) {
      if (!fields.has(fieldName) || assigned.has(fieldName)) return false;
      assigned.add(fieldName);
    }
    if (hasOwn(fieldset, 'title') && !safeText(fieldset.title, TEXT_LIMITS.title)) return false;
    if (hasOwn(fieldset, 'description') && !safeText(fieldset.description, TEXT_LIMITS.description))
      return false;
  }
  return true;
}

/**
 * Validate server-provided, presentation-only Plugin UI metadata again at the
 * browser boundary. Invalid metadata returns null, producing the existing
 * generic form rather than a partially interpreted layout.
 */
export function normalizePluginUISchema(raw: any, parameters: any): UISchema | null {
  if (raw === undefined || raw === null || (isRecord(raw) && !Object.keys(raw).length)) return null;
  if (!isRecord(raw) || !isSafeJsonTree(raw) || !hasOnlyKeys(raw, TOP_KEYS)) return null;
  let serialized: string;
  try {
    serialized = JSON.stringify(raw);
  } catch {
    return null;
  }
  if (!serialized || new TextEncoder().encode(serialized).length > MAX_SCHEMA_BYTES) return null;
  raw = JSON.parse(serialized);

  const definitions = collectParameters(parameters);
  if (
    !definitions ||
    !hasOwn(raw, 'version') ||
    raw.version !== 1 ||
    !['standard', 'wide'].includes(ownOr(raw, 'layout', 'standard'))
  ) {
    return null;
  }
  if (hasOwn(raw, 'title') && !safeText(raw.title, TEXT_LIMITS.title)) return null;
  if (hasOwn(raw, 'description') && !safeText(raw.description, TEXT_LIMITS.description)) return null;

  const sections = ownOr(raw, 'sections', []);
  if (!Array.isArray(sections) || sections.length > MAX_SECTIONS) return null;
  const sectionIds = new Set();
  const assigned = new Set();
  for (const section of sections) {
    if (
      !isRecord(section) ||
      !hasOnlyKeys(section, SECTION_KEYS) ||
      !hasOwn(section, 'id') ||
      typeof section.id !== 'string' ||
      !ID_PATTERN.test(section.id) ||
      sectionIds.has(section.id) ||
      !hasOwn(section, 'parameters') ||
      !Array.isArray(section.parameters) ||
      section.parameters.some((name: any) => typeof name !== 'string') ||
      ![1, 2].includes(ownOr(section, 'columns', 1)) ||
      typeof ownOr(section, 'collapsed', false) !== 'boolean' ||
      !TONES.has(ownOr(section, 'tone', 'default'))
    )
      return null;
    sectionIds.add(section.id);
    if (new Set(section.parameters).size !== section.parameters.length) return null;
    for (const parameterName of section.parameters) {
      if (!definitions.has(parameterName) || assigned.has(parameterName)) return null;
      assigned.add(parameterName);
    }
    if (hasOwn(section, 'title') && !safeText(section.title, TEXT_LIMITS.title)) return null;
    if (hasOwn(section, 'description') && !safeText(section.description, TEXT_LIMITS.description))
      return null;
  }

  const presentations = ownOr(raw, 'parameters', {});
  if (!isRecord(presentations) || Object.keys(presentations).length > MAX_PARAMETERS) return null;
  for (const [parameterName, presentation] of Object.entries(presentations)) {
    const parameter = definitions.get(parameterName);
    if (!parameter) return null;
    const type = String(ownOr(parameter, 'type', 'str')).toLowerCase();
    if (!validatePresentation(presentation, type, parameterName, { objectArray: type === 'object_array' }))
      return null;
    if (type === 'object_array' && !validateObjectArrayPresentation(presentation, parameter)) return null;
  }

  return raw;
}

export interface UISection {
  id: string;
  title?: string;
  description?: string;
  parameters: string[];
  columns?: 1 | 2;
  collapsed?: boolean;
  tone?: 'default' | 'accent' | 'muted';
}

export interface UIFieldset {
  id: string;
  title?: string;
  description?: string;
  fields: string[];
  collapsed?: boolean;
}

export interface UIPresentation {
  label?: string;
  help?: string;
  span?: number;
  placeholder?: string;
  widget?: 'text' | 'url' | 'path' | 'code' | 'textarea' | 'switch';
  unit?: string;
  true_label?: string;
  false_label?: string;
  add_label?: string;
  item_placeholder?: string;
  empty_title?: string;
  empty_description?: string;
  item_title_field?: string;
  item_fallback_field?: string;
  item_subtitle_field?: string;
  item_badge_field?: string;
  item_status_field?: string;
  fields?: Record<string, UIPresentation>;
  fieldsets?: UIFieldset[];
}

export interface UISchema {
  version: 1;
  layout?: 'standard' | 'wide';
  title?: string;
  description?: string;
  sections?: UISection[];
  parameters?: Record<string, UIPresentation>;
}
