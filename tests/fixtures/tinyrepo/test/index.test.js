'use strict';
const test = require('node:test');
const assert = require('node:assert');
const lib = require('../lib');

test('join caps at three items', () => {
  assert.strictEqual(lib.join(['a', 'b', 'c', 'd']), 'a, b, c');
});
test('join with a custom separator', () => {
  assert.strictEqual(lib.join(['x', 'y'], '-'), 'x-y');
});
test('mutually recursive even/odd', () => {
  assert.strictEqual(lib.isEven(4), true);
  assert.strictEqual(lib.isOdd(7), true);
});
