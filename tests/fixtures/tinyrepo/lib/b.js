'use strict';
const a = require('./a');

function isOdd(n) {
  return n === 0 ? false : a.isEven(n - 1);
}

exports.isOdd = isOdd;
