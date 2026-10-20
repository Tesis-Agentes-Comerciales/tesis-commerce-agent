// Hermetic entrypoint audit: executes the unchanged TSX entry with module doubles.
// No browser, application runtime, authentication, network or commercial action.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('../../ui/node_modules/typescript');
const entry = readFileSync(resolve(__dirname, '../../ui/src/main.tsx'), 'utf8');
// Vite inlines import.meta.env at build time; `buildEnv` stands in for that object.
assert.equal(entry.split('import.meta.env').length, 2);
const source = ts.transpileModule(entry.replace('import.meta.env', 'buildEnv'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX },
}).outputText;

async function selectEntry(pathname, buildEnv) {
  let loader;
  let renders = 0;
  const imported = [];
  const document = { title: 'Original title', getElementById: (id) => { assert.equal(id, 'root'); return {}; } };
  const context = {
    exports: {}, window: { location: { pathname } }, document, buildEnv,
    require: (name) => {
      imported.push(name);
      if (name === 'react') return { StrictMode: 'StrictMode', Suspense: 'Suspense', lazy: (fn) => { loader = fn; return 'LazyEntry'; } };
      if (name === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }) };
      if (name === 'react-dom/client') return { createRoot: () => ({ render: () => { renders++; } }) };
      if (name === './index.css') return {};
      if (name === './App.tsx' || name === './features/governance-monitor/route.tsx') return { __esModule: true, default: name };
      throw Error(`Unexpected import: ${name}`);
    },
    fetch: () => { throw Error('Network forbidden'); },
  };
  vm.runInNewContext(source, context, { timeout: 2000 });
  assert.equal(renders, 1);
  const selected = (await loader()).default;
  assert.deepEqual(imported.filter((name) => name.startsWith('./') && name !== './index.css'), [selected]);
  assert.equal(document.title, 'Original title');
  return selected;
}

for (const pathname of ['/', '/governance', '/governance/', '/settings', '/governance-other', '/governance/sub', '/signin', '/governance//']) {
  test(`entry route ${pathname} selects one module without commerce execution`, async () => {
    const monitor = pathname === '/governance' || pathname === '/governance/';
    assert.equal(await selectEntry(pathname, { VITE_GOVERNANCE_MONITOR: 'true' }), monitor ? './features/governance-monitor/route.tsx' : './App.tsx');
    assert.equal(await selectEntry(pathname, {}), './App.tsx');
  });
}
test('only the explicit build flag value true mounts the monitor', async () => {
  for (const value of [undefined, '', 'false', '1', 'TRUE', ' true']) {
    assert.equal(await selectEntry('/governance', { VITE_GOVERNANCE_MONITOR: value }), './App.tsx', String(value));
  }
});
