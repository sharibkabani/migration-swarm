'use strict';
import constants = require('./constants');

/** Join at most MAX items. */
function join(items: readonly string[], sep?: string): string {
  const s = sep === undefined ? constants.DEFAULT_SEP : sep;
  return items.slice(0, constants.MAX).join(s);
}

export = { join };
