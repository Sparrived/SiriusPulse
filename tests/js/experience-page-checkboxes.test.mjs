import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/pages/experience.js', 'utf8');

assert.match(
  source,
  /loadExperience\(name, autoSave\)/,
  'loadExperience should receive autoSave from init instead of closing over an undefined binding',
);

for (const field of [
  'enable_skills',
  'plan_mode_enabled',
  'plan_mode_limit_normal_tools',
  'plan_mode_allow_light_chat',
  'plan_mode_chat_awareness_enabled',
  'plan_mode_presence_enabled',
]) {
  assert.match(source, new RegExp(`data-boolean-field="\\$\\{name\\}"`));
  assert.equal(
    source.includes(`select name="${field}"`),
    false,
    `${field} should not be rendered as a true/false select`,
  );
  assert.equal(
    source.includes(`form.${field}.value === 'true'`),
    false,
    `${field} should not be saved from select values`,
  );
}

assert.equal(source.includes('type="checkbox"'), false);
assert.match(source, /BOOLEAN_FIELDS\.forEach\(name => \{\n\s+experience\[name\] = Boolean\(booleanState\[name\]\);/);
assert.match(source, /function setupBooleanCards\(root, autoSave\)/);
assert.match(source, /行为画像/);
assert.match(source, /回复控制/);
assert.equal(source.includes('reply_time_curve_enabled'), false);
assert.match(source, /reply_time_curve_points: normalizeCurvePoints\(replyTimeCurvePoints\)/);
assert.match(source, /最终参与分数 = 原始 score × 当前时间系数/);
assert.match(source, /始终启用/);
assert.match(source, /工具与计划/);
assert.match(source, /记忆检索/);
assert.match(source, /memory_unit_top_k/);
assert.match(source, /记忆单元 Top-K/);
