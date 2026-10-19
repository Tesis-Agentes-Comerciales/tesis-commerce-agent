import { readFileSync, existsSync } from "node:fs"
import { registerHooks } from "node:module"
import ts from "typescript"

const src = new URL("../src/", import.meta.url)
// Compile only local TS/TSX for SSR assertions; use existing locked TypeScript, no test dependency.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      const base = new URL(specifier.slice(2), src)
      const candidate = [base, new URL(base.href + ".tsx"), new URL(base.href + ".ts")].find((p) => existsSync(p))
      if (!candidate) throw Error("Unresolved UI alias")
      return nextResolve(candidate.href, context)
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.startsWith(src.href) && /\.tsx?$/.test(url)) {
      const { outputText } = ts.transpileModule(readFileSync(new URL(url), "utf8"), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX },
      })
      return { format: "module", shortCircuit: true, source: outputText }
    }
    return nextLoad(url, context)
  },
})
