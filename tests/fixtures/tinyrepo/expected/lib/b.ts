'use strict';
import a = require('./a');

export function isOdd(n: number): boolean {
  return n === 0 ? false : a.isEven(n - 1);
}
