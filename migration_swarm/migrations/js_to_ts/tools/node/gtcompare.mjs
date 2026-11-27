// Ground-truth type comparison for the js-to-ts eval. Runs inside the msw-js-tools container.
//
// Input (JSON file): { pairs: [{module, ours, gt}], result }   (ours/gt: absolute .d.ts paths)
// One TypeScript program contains both sides, so types can be compared with the checker.
//
// Definition, per exported symbol name present in both modules:
//   type compared   value exports: the type of the symbol (typeof x); type-only exports: the declared type
//   equivalent      ours assignable to gt AND gt assignable to ours, AND ours' printed type has no `any`
//                   (lenient: e.g. a non-generic signature can be equivalent to a generic one)
//   identical       equivalent AND the printed types are the same string (strict)
//   any_weakened    mutually assignable only because ours uses `any` somewhere in the type
//   wider/narrower  assignable in one direction only (gt -> ours = ours is wider)
//   incomparable    neither direction
//   missing         exported by gt, not by ours (coverage)

import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const req = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

const roots = req.pairs.flatMap((p) => [p.ours, p.gt]).filter((f) => fs.existsSync(f));
const program = ts.createProgram(roots, {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
  strict: true, skipLibCheck: true, noEmit: true, esModuleInterop: true, allowJs: false,
});
const checker = program.getTypeChecker();
if (typeof checker.isTypeAssignableTo !== "function") throw new Error("checker.isTypeAssignableTo unavailable");

function exportsOf(file) {
  const sf = program.getSourceFile(file);
  const out = new Map();
  if (!sf) return out;
  const mod = checker.getSymbolAtLocation(sf);
  if (!mod) return out;
  // `export = X`: compare X itself and each of its properties (CommonJS outputs vs ES-export humans).
  const eq = mod.exports && mod.exports.get("export=");
  if (eq) {
    const target = eq.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(eq) : eq;
    const decl = target.valueDeclaration || (target.declarations || [])[0];
    if (decl && target.flags & ts.SymbolFlags.Value) {
      const t = checker.getTypeOfSymbolAtLocation(target, decl);
      out.set("export=", { type: t, kind: "value" });
      for (const prop of checker.getPropertiesOfType(t)) {
        const pd = prop.valueDeclaration || (prop.declarations || [])[0];
        if (pd) out.set(prop.name, { type: checker.getTypeOfSymbolAtLocation(prop, pd), kind: "value" });
      }
    }
  }
  for (const sym of checker.getExportsOfModule(mod)) {
    if (sym.name === "export=" || out.has(sym.name)) continue;
    const s = sym.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(sym) : sym;
    const decl = s.valueDeclaration || (s.declarations || [])[0];
    if (!decl) continue;
    let type, kind;
    if (s.flags & ts.SymbolFlags.Value) {
      type = checker.getTypeOfSymbolAtLocation(s, decl);
      kind = "value";
    } else if (s.flags & ts.SymbolFlags.Type) {
      type = checker.getDeclaredTypeOfSymbol(s);
      kind = "type";
    } else continue;
    out.set(sym.name, { type, kind });
  }
  return out;
}

const hasAny = (t) => /\bany\b/.test(checker.typeToString(t, undefined, ts.TypeFormatFlags.NoTruncation));
const results = [];
for (const p of req.pairs) {
  const ours = exportsOf(p.ours);
  const gt = exportsOf(p.gt);
  for (const [name, g] of gt) {
    const o = ours.get(name);
    if (!o) { results.push({ module: p.module, name, kind: g.kind, verdict: "missing" }); continue; }
    const o2g = checker.isTypeAssignableTo(o.type, g.type);
    const g2o = checker.isTypeAssignableTo(g.type, o.type);
    let verdict = o2g && g2o ? (hasAny(o.type) ? "any_weakened" : "equivalent") : g2o ? "wider" : o2g ? "narrower" : "incomparable";
    const os = checker.typeToString(o.type, undefined, ts.TypeFormatFlags.NoTruncation);
    const gs = checker.typeToString(g.type, undefined, ts.TypeFormatFlags.NoTruncation);
    results.push({
      module: p.module, name, kind: g.kind, verdict, identical: verdict === "equivalent" && os === gs,
      ours: checker.typeToString(o.type).slice(0, 300), gt: checker.typeToString(g.type).slice(0, 300),
    });
  }
}
fs.writeFileSync(req.result, JSON.stringify({ results }));
