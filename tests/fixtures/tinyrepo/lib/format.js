'use strict';
const constants = require('./constants');

/** Join at most MAX items. */
function join(items, sep) {
  const s = sep === undefined ? constants.DEFAULT_SEP : sep;
  return items.slice(0, constants.MAX).join(s);
}

module.exports = { join };
