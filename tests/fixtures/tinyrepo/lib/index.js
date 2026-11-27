'use strict';
const format = require('./format');
const a = require('./a');
const b = require('./b');

module.exports = { join: format.join, isEven: a.isEven, isOdd: b.isOdd };
