import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../../sirius_pulse/webui/static/pages/skills.js', import.meta.url), 'utf8');

assert.match(source, /const modal\$ = \(id\) => currentModal\?\.querySelector\(`#\$\{id\}`\);/);
assert.match(source, /overlay\.querySelector\('#modalClose'\)\.addEventListener/);
assert.match(source, /meta\.config_parameters\?\.length \? meta\.config_parameters/);
assert.doesNotMatch(source, /(?<![A-Za-z0-9_$])\$\('modal(?:Close|Cancel|Body|Save|cfgEnabled|cfgExtra)'\)/);
