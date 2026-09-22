import assert from 'node:assert/strict';
import {renderPixels} from '../research_assistant/web/static/reader/render_quality.js';
for (const dpr of [1, 1.25, 2, 3]) {
  const size = renderPixels(500, 700, dpr);
  assert.ok(size.width >= 1000 && size.height >= 1400);
  assert.equal(size.transform[0], size.width / 500);
  assert.equal(size.transform[3], size.height / 700);
}
const huge = renderPixels(20000, 30000, 3);
assert.ok(huge.width * huge.height <= 16000000);
assert.ok(Math.max(huge.width, huge.height) <= 8192);
console.log('5 rendering density/memory checks passed');
