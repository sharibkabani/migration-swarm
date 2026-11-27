'use strict';
const b = require('./b');

function isEven(n) {
  return n === 0 ? true : b.isOdd(n - 1);
}

exports.isEven = isEven;
