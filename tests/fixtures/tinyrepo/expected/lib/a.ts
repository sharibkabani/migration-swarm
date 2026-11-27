'use strict';
import b = require('./b');

export function isEven(n: number): boolean {
  return n === 0 ? true : b.isOdd(n - 1);
}
