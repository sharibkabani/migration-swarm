// One TypeScript program pass for the js-to-ts judge. Runs inside the msw-js-tools container.
//
// Input: path to a JSON request file:
//   { project, outDir, buildInfo, emit, escapeFiles: [rel .ts paths], normalize: [rel .js paths under outDir], normDir, result }
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
  const res = program.emit(undefined, (fileName, text) => {
    fs.mkdirSync(path.dirname(fileName), { recursive: true });
    fs.writeFileSync(fileName, text);
    emitted.push(path.relative(req.outDir, fileName).split(path.sep).join("/"));
  });
  all.push(...res.diagnostics);
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

// Normalized runtime JS for drift comparison: reprint emitted JS without comments.
const printer = ts.createPrinter({ removeComments: true, newLine: ts.NewLineKind.LineFeed });
for (const f of req.normalize || []) {
  const src = path.join(req.outDir, f);
  if (!fs.existsSync(src)) continue;
  const sf = ts.createSourceFile(src, fs.readFileSync(src, "utf8"), ts.ScriptTarget.Latest, false, ts.ScriptKind.JS);
  const dest = path.join(req.normDir, f);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, printer.printFile(sf));
}

fs.writeFileSync(req.result, JSON.stringify({ diagnostics, escapes, emitted }));
