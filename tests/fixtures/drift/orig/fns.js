'use strict';
exports.f = function (a) { return a + 1; };
exports.g = function () { return exports.f(1); };
module.exports.h = function () { return 2; };
