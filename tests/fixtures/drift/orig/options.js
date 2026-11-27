'use strict';
exports.default = { xml: false, decodeEntities: true };
const xmlModeDefault = { _useHtmlParser2: true, xmlMode: true };
exports.flatten = function (options) {
  return options && options.xml ? xmlModeDefault : options;
};
