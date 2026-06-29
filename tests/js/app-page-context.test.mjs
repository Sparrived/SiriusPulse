import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/app.js', 'utf8');

assert.match(source, /import \{ createPageContext \} from '\.\/page-context\.js';/);
assert.match(source, /let activePageAbortController = null;/);
assert.match(source, /activePageAbortController\?\.abort\(\);/);
assert.match(source, /new AbortController\(\)/);
assert.match(source, /createPageContext\(\{ container: main, signal: activePageAbortController\.signal \}\)/);
assert.match(source, /await initFn\(main, \{ page, name, ctx: pageContext, signal: pageContext\.signal \}\)/);
