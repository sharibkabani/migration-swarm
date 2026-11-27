'use strict';
exports.f = function () { return this.transform(value => (value != null ? value | 0 : value)); };
exports.g = function (a, b, c) { return (a + b) * c; };
