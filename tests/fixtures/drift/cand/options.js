"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.flatten = void 0;
exports.default = { xml: false, decodeEntities: true };
const xmlModeDefault = { _useHtmlParser2: true, xmlMode: true };
const flatten = function (options) {
    return options && options.xml ? xmlModeDefault : options;
};
exports.flatten = flatten;
