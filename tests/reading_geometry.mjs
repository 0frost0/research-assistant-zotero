import assert from 'node:assert/strict';
import {selectionLines} from '../research_assistant/web/static/reader/selection_geometry.js';
// Three neighboring Chinese glyphs belong to one line, even if the first and
// last glyph are separated by more than the gutter threshold.
assert.deepEqual(selectionLines([[0,0,10,10],[10,0,20,10],[20,0,30,10],[30,0,40,10]]),[[0,0,40,10]]);
assert.equal(selectionLines([[0,40,40,50],[0,20,40,30],[0,0,40,10]]).length,3);
assert.throws(()=>selectionLines([[0,0,40,10],[90,0,130,10]]),/跨栏/);
console.log('3 selection geometry checks passed');
