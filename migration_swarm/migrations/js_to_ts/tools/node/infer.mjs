// Produce advisory drafts in memory. Only the active JS file is viewed as TypeScript.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const ts = createRequire(import.meta.url)("typescript");
const [project, manifest] = process.argv.slice(2);
const root = process.cwd();
const config = ts.readConfigFile(project, ts.sys.readFile);
if (config.error) throw Error(ts.flattenDiagnosticMessageText(config.error.messageText, "\n"));
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root);
const roots = parsed.fileNames.map(f => path.resolve(f));
let source, target, draft;
let version = 0;
const service = ts.createLanguageService({
  ...ts.sys,
  useCaseSensitiveFileNames: () => ts.sys.useCaseSensitiveFileNames,
  getScriptFileNames: () => [...roots.filter(f => f !== source), target],
  getScriptVersion: f => f === target ? String(version) : "0",
  getProjectVersion: () => String(version),
  getCurrentDirectory: () => root,
  getCompilationSettings: () => parsed.options,
  getDefaultLibFileName: ts.getDefaultLibFilePath,
  fileExists: f => f === target || (f !== source && ts.sys.fileExists(f)),
  getScriptSnapshot(f) {
    const text = f === target ? draft : f === source ? undefined : ts.sys.readFile(f);
    return text === undefined ? undefined : ts.ScriptSnapshot.fromString(text);
  },
});

const seeds = Object.create(null);
for (const file of JSON.parse(fs.readFileSync(manifest, "utf8")).files) {
  source = path.resolve(file);
  target = source.replace(/\.([cm]?)js$/, ".$1ts");
  if (source === target || ts.sys.fileExists(target)) continue;
  draft = ts.sys.readFile(source);
  if (draft === undefined) continue;
  version++;
  const before = service.getSemanticDiagnostics(target);
  const parameter = before.find(d => d.code === 7006);
  if (!parameter) continue;
  const fixes = service.getCodeFixesAtPosition(target, parameter.start, parameter.start + parameter.length,
                                               [parameter.code], {}, {});
  const fix = fixes.find(f => f.fixName === "inferFromUsage" && f.fixId);
  if (!fix) continue;
  const batch = service.getCombinedCodeFix({ type: "file", fileName: target }, fix.fixId, {}, {});
  const changes = batch.changes.find(c => c.fileName === target)?.textChanges ?? [];
  // Accept only standalone annotation insertions. A fix can also propose expressions or imports.
  const annotations = changes.filter(c => c.span.length === 0 && /^\s*:\s*\S/.test(c.newText)
                                          && !/\bany\b/.test(c.newText));
  if (!annotations.length) continue;
  for (const {span, newText} of annotations.sort((a, b) => b.span.start - a.span.start)) {
    draft = draft.slice(0, span.start) + newText + draft.slice(span.start);
  }
  version++;
  if (service.getSyntacticDiagnostics(target).length) continue;
  const after = service.getSemanticDiagnostics(target);
  if (after.length >= before.length) continue;
  const rel = path.relative(root, target);
  seeds[file] = {
    files: { [rel]: draft }, before: before.length, after: after.length, annotations: annotations.length,
    reasons: after.slice(0, 30).map(d => {
      const {line, character} = d.file.getLineAndCharacterOfPosition(d.start);
      return `${rel}:${line + 1}:${character + 1} TS${d.code}: ${ts.flattenDiagnosticMessageText(d.messageText, "\n")}`;
    }),
  };
}
process.stdout.write(JSON.stringify(seeds));
