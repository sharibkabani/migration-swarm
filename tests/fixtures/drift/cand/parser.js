'use strict';
Object.defineProperty(exports, "__esModule", { value: true });
exports.render = exports.parse = void 0;
exports.parse = require("htmlparser2")
    .parseDocument;
exports.render = require("dom-serializer").default;
