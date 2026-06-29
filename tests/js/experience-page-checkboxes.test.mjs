import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/pages/experience.js', 'utf8');

for (const field of [
  'enable_skills',
  'plan_mode_enabled',
  'plan_mode_limit_normal_tools',
  'plan_mode_allow_light_chat',
  'plan_mode_chat_awareness_enabled',
  'plan_mode_presence_enabled',
]) {
  assert.match(source, new RegExp(`type="checkbox" name="\\$\\{name\\}"`));
  assert.equal(
    source.includes(`select name="${field}"`),
    false,
    `${field} should not be rendered as a true/false select`,
  );
  assert.equal(
    source.includes(`form.${field}.value === 'true'`),
    false,
    `${field} should be saved from checkbox checked state`,
  );
}

assert.match(source, /BOOLEAN_FIELDS\.forEach\(name => \{\n\s+experience\[name\] = Boolean\(form\[name\]\?\.checked\);/);
assert.match(source, /行为画像/);
assert.match(source, /回复控制/);
assert.match(source, /工具与计划/);
assert.match(source, /记忆检索/);