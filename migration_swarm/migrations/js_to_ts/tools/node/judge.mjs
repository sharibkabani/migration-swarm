// One TypeScript program pass for the js-to-ts judge. Runs inside the msw-js-tools container.
//
// Input: path to a JSON request file:
//   { project, outDir, buildInfo, emit, escapeFiles: [rel .ts paths], normalize: [rel .js paths under outDir], normDir, result }
// normalize: emitted .js files are written to normDir as canonical token streams (one token per line).
// Output: JSON written to `result`:
//   { diagnostics: [{file, line, col, code, message}], escapes: {file: {counts, sites}}, emitted: [rel paths] }
//
// Escape counting is done on the AST (never regex over source text). Comment directives are found
// through the comment ranges attached to AST nodes.

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");

const req = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const projectDir = path.dirname(req.project);
const rel = (f) => path.relative(projectDir, f).split(path.sep).join("/");

// TS diagnostics that mean "this would be implicitly `any`" (noImplicitAny family).
const IMPLICIT_ANY = new Set([7005, 7006, 7008, 7010, 7011, 7015, 7016, 7017, 7018, 7019, 7022, 7023, 7024, 7031, 7034, 7041, 7053]);

const cfg = ts.readConfigFile(req.project, ts.sys.readFile);
if (cfg.error) {
  fs.writeFileSync(req.result, JSON.stringify({ fatal: ts.flattenDiagnosticMessageText(cfg.error.messageText, "\n") }));
  process.exit(0);
}
const overrides = {
  outDir: req.outDir,
  rootDir: projectDir,
  noEmit: !req.emit,
  noEmitOnError: false,
  declaration: !!req.emit,
  declarationMap: false,
  sourceMap: false,
  incremental: true,
  tsBuildInfoFile: req.buildInfo,
  composite: false,
};
const parsed = ts.parseJsonConfigFileContent(cfg.config, ts.sys, projectDir, overrides, req.project);
const host = ts.createIncrementalCompilerHost(parsed.options);
const program = ts.createIncrementalProgram({
  rootNames: parsed.fileNames,
  options: parsed.options,
  host,
  configFileParsingDiagnostics: ts.getConfigFileParsingDiagnostics(parsed),
  projectReferences: parsed.projectReferences,
});

const all = [
  ...program.getConfigFileParsingDiagnostics(),
  ...program.getOptionsDiagnostics(),
  ...program.getGlobalDiagnostics(),
  ...program.getSyntacticDiagnostics(),
  ...program.getSemanticDiagnostics(),
];
const emitted = [];
if (req.emit) {
  // Emit diagnostics are not counted: they come from the .d.ts emit we add for worker context
  // (e.g. TS9005 on not-yet-migrated JS), not from the code's type correctness.
  program.emit(undefined, (fileName, text) => {
    fs.mkdirSync(path.dirname(fileName), { recursive: true });
    fs.writeFileSync(fileName, text);
    emitted.push(path.relative(req.outDir, fileName).split(path.sep).join("/"));
  });
}

const diagnostics = all
  .filter((d) => d.category === ts.DiagnosticCategory.Error)
  .map((d) => {
    const out = { file: null, line: 0, col: 0, code: d.code, message: ts.flattenDiagnosticMessageText(d.messageText, "\n") };
    if (d.file && d.start !== undefined) {
      const { line, character } = d.file.getLineAndCharacterOfPosition(d.start);
      Object.assign(out, { file: rel(d.file.fileName), line: line + 1, col: character + 1 });
    }
    return out;
  });

function unwrapParens(e) {
  while (ts.isParenthesizedExpression(e)) e = e.expression;
  return e;
}

// `require("x") as typeof import("x")` is a precise typing of a CommonJS require, not an escape.
function isTypedRequire(node) {
  const call = unwrapParens(node.expression);
  const t = node.type;
  return (
    ts.isCallExpression(call) && ts.isIdentifier(call.expression) && call.expression.text === "require" &&
    call.arguments.length === 1 && ts.isStringLiteralLike(call.arguments[0]) &&
    ts.isImportTypeNode(t) && t.isTypeOf && ts.isLiteralTypeNode(t.argument) &&
    ts.isStringLiteralLike(t.argument.literal) && t.argument.literal.text === call.arguments[0].text
  );
}

function countEscapes(sf, fileDiags) {
  const counts = { explicit_any: 0, implicit_any: 0, ts_ignore: 0, ts_expect_error: 0, ts_nocheck: 0, as_unknown_as: 0, non_null_assertion: 0, as_cast: 0 };
  const sites = [];
  const hit = (kind, pos) => {
    counts[kind]++;
    sites.push({ kind, line: sf.getLineAndCharacterOfPosition(pos).line + 1 });
  };
  const text = sf.getFullText();
  const seenComments = new Set();
  const scanComments = (ranges) => {
    for (const r of ranges || []) {
      if (seenComments.has(r.pos)) continue;
      seenComments.add(r.pos);
      const body = text.slice(r.pos, r.end).replace(/^\/\/+|^\/\*+|\*+\/$/g, "").replace(/^[\s*]*/, "");
      if (body.startsWith("@ts-ignore")) hit("ts_ignore", r.pos);
      else if (body.startsWith("@ts-expect-error")) hit("ts_expect_error", r.pos);
      else if (body.startsWith("@ts-nocheck")) hit("ts_nocheck", r.pos);
    }
  };
  const visit = (node) => {
    scanComments(ts.getLeadingCommentRanges(text, node.pos));
    scanComments(ts.getTrailingCommentRanges(text, node.end));
    if (node.kind === ts.SyntaxKind.AnyKeyword) hit("explicit_any", node.getStart(sf));
    else if (ts.isNonNullExpression(node)) hit("non_null_assertion", node.getStart(sf));
    else if (ts.isAsExpression(node) || ts.isTypeAssertionExpression(node)) {
      const t = node.type;
      const isConst = ts.isTypeReferenceNode(t) && ts.isIdentifier(t.typeName) && t.typeName.text === "const";
      const inner = unwrapParens(node.expression);
      const innerIsUnknown = (ts.isAsExpression(inner) || ts.isTypeAssertionExpression(inner)) && inner.type.kind === ts.SyntaxKind.UnknownKeyword;
      const parent = node.parent && ts.isParenthesizedExpression(node.parent) ? node.parent.parent : node.parent;
      const isInnerOfDouble = t.kind === ts.SyntaxKind.UnknownKeyword && parent && (ts.isAsExpression(parent) || ts.isTypeAssertionExpression(parent));
      if (isConst || isInnerOfDouble || isTypedRequire(node)) {
        // not an escape (or counted at the outer node)
      } else if (innerIsUnknown) hit("as_unknown_as", node.getStart(sf));
      else hit("as_cast", node.getStart(sf));
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  scanComments(ts.getLeadingCommentRanges(text, sf.endOfFileToken.pos));
  for (const d of fileDiags) {
    if (IMPLICIT_ANY.has(d.code)) {
      counts.implicit_any++;
      sites.push({ kind: "implicit_any", line: d.line });
    }
  }
  return { counts, sites };
}

const escapes = {};
for (const f of req.escapeFiles || []) {
  const sf = program.getSourceFile(path.join(projectDir, f));
  if (!sf) {
    escapes[f] = { missing: true };
    continue;
  }
  escapes[f] = countEscapes(sf, diagnostics.filter((d) => d.file === f));
}

// Canonical runtime token stream of emitted JS, for drift comparison against the original.
// Leaf tokens of the AST (comments and whitespace are not tokens), with CommonJS export plumbing
// folded so that equivalent export forms compare equal:
//   dropped statements: "use strict", Object.defineProperty(exports, "__esModule", ...),
//                       exports.a = exports.b = void 0, exports.X = X, export {}
//   rewrites: module.exports.X / exports.X / (0, exports.X) -> X ; `function X (` -> `X = function (` ;
//             const/let/var and `;` dropped. Applied identically to both sides.
function leafTokens(node, sf, out) {
  const kids = node.getChildren(sf);
  if (kids.length === 0) {
    if (node.kind === ts.SyntaxKind.EndOfFileToken) return;
    out.push(ts.isStringLiteral(node) ? JSON.stringify(node.text) : node.getText(sf));
    return;
  }
  for (const k of kids) {
    // JSDoc is parsed into nodes for JS files; it is a comment, not runtime code.
    if (k.kind >= ts.SyntaxKind.FirstJSDocNode && k.kind <= ts.SyntaxKind.LastJSDocNode) continue;
    leafTokens(k, sf, out);
  }
}
function isPlumbing(toks) {
  const s = toks.join(" ");
  return s === '"use strict" ;' || s === '"use strict"' || s === "export { } ;" ||
    s.startsWith('Object . defineProperty ( exports , "__esModule"') ||
    /^exports \. [\w$]+ = (exports \. [\w$]+ = )*void 0 ;?$/.test(s) ||
    /^exports \. ([\w$]+) = \1 ;?$/.test(s);
}
// Drop every parenthesized expression; the factory's update functions re-insert exactly the
// parentheses that precedence requires, so redundant parens vanish but `(a + b) * c` survives.
function stripRedundantParens(sf) {
  const visit = (ctx) => (node) => {
    while (ts.isParenthesizedExpression(node)) node = node.expression;
    return ts.visitEachChild(node, visit(ctx), ctx);
  };
  const res = ts.transform(sf, [(ctx) => (root) => ts.visitEachChild(root, visit(ctx), ctx)]);
  const printed = ts.createPrinter({ removeComments: true }).printFile(res.transformed[0]);
  res.dispose();
  return printed;
}
function canonicalTokens(text) {
  const sf0 = ts.createSourceFile("x.js", text, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const sf = ts.createSourceFile("x.js", stripRedundantParens(sf0), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const toks = [];
  for (const st of sf.statements) {
    const t = [];
    leafTokens(st, sf, t);
    if (!isPlumbing(t)) toks.push(...t);
  }
  const out = [];
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i];
    if (t === "module" && toks[i + 1] === "." && toks[i + 2] === "exports" && toks[i + 3] === ".") { i += 1; continue; }
    if (t === "(" && toks[i + 1] === "0" && toks[i + 2] === "," && toks[i + 3] === "exports" && toks[i + 4] === "." && toks[i + 6] === ")") {
      out.push(toks[i + 5]); i += 6; continue;
    }
    if (t === "exports" && toks[i + 1] === "." && /^[\w$]+$/.test(toks[i + 2] || "")) { out.push(toks[i + 2]); i += 2; continue; }
    if (t === "function" && /^[\w$]+$/.test(toks[i + 1] || "") && toks[i + 2] === "(") { out.push(toks[i + 1], "=", "function"); i += 1; continue; }
    if (t === "(" && /^[\w$]+$/.test(toks[i + 1] || "") && toks[i + 2] === ")" && toks[i + 3] === "=>") { out.push(toks[i + 1]); i += 2; continue; }
    if (t === "const" || t === "let" || t === "var" || t === ";") continue;
    out.push(t);
  }
  return out;
}
for (const f of req.normalize || []) {
  const src = path.join(req.outDir, f);
  if (!fs.existsSync(src)) continue;
  const dest = path.join(req.normDir, f);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, canonicalTokens(fs.readFileSync(src, "utf8")).join("\n") + "\n");
}

// transpileTokens: source files (relative to the project dir, JS or TS) transpiled on their own with
// the project's options and written to normDir/<path>.tok as canonical tokens. Used to check that a
// test file renamed to .ts differs from the original only by types.
for (const f of req.transpileTokens || []) {
  const src = path.join(projectDir, f);
  if (!fs.existsSync(src)) continue;
  const out = ts.transpileModule(fs.readFileSync(src, "utf8"), {
    compilerOptions: { ...parsed.options, noEmit: false, declaration: false, sourceMap: false }, fileName: src,
  });
  const dest = path.join(req.normDir, f + ".tok");
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, canonicalTokens(out.outputText).join("\n") + "\n");
}

// Exported names of npm packages, from their installed type declarations (so workers do not guess).
const packageExports = {};
if ((req.listExports || []).length) {
  const roots = {};
  for (const spec of req.listExports) {
    const r = ts.resolveModuleName(spec, path.join(projectDir, "__msw_probe__.ts"), parsed.options, ts.sys);
    const f = r.resolvedModule && r.resolvedModule.resolvedFileName;
    if (f && /\.d\.[cm]?ts$/.test(f)) roots[spec] = f;
    else packageExports[spec] = { unresolved: true };
  }
  const p2 = ts.createProgram(Object.values(roots), { ...parsed.options, noEmit: true });
  const checker = p2.getTypeChecker();
  for (const [spec, f] of Object.entries(roots)) {
    const sf = p2.getSourceFile(f);
    const sym = sf && checker.getSymbolAtLocation(sf);
    if (!sym) { packageExports[spec] = { unresolved: true }; continue; }
    const types = [], values = [];
    for (const e of checker.getExportsOfModule(sym)) {
      const t = e.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(e) : e;
      if (t.flags & ts.SymbolFlags.Value) values.push(e.name);
      else if (t.flags & ts.SymbolFlags.Type) types.push(e.name);
    }
    packageExports[spec] = { types: types.sort(), values: values.sort() };
  }
}

fs.writeFileSync(req.result, JSON.stringify({ diagnostics, escapes, emitted, packageExports }));
