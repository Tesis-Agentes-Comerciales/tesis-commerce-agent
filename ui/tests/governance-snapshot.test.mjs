import './monitor-test-loader.mjs';
import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import http from 'node:http';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { readFile, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createDemoServer, validateSnapshotBytes, assertDevelopmentRuntime } from '../../scripts/governance_snapshot_server.mjs';
import { preparedFixtureBytes } from '../../scripts/prepare_governance_snapshot.mjs';
import { parseDemoSnapshot, MAX_SNAPSHOT_BYTES } from '../src/features/governance-monitor/snapshot-contract.ts';
import { demoConfig } from '../src/features/governance-monitor/demo-config.ts';
import { DEMO_SNAPSHOT_SHA256 } from '../src/features/governance-monitor/snapshot-pin.ts';
import { parseEvidence } from '../src/features/governance-monitor/adapter.ts';
import { loadLocalSnapshot, SnapshotReadError, DEMO_SNAPSHOT_URL } from '../src/features/governance-monitor/snapshot-reader.ts';
import { offlineSource, beginLocalRead, acceptLocalRead, rejectLocalRead } from '../src/features/governance-monitor/source-state.ts';
const { SourceView } = await import('../src/features/governance-monitor/SourceMonitor.tsx');

// Prepared from the versioned evidence on every run: no `.local` cache or earlier preparation step.
const bytes = await preparedFixtureBytes();
const snapshot = parseDemoSnapshot(JSON.parse(bytes));
const offline = parseEvidence(JSON.parse(await readFile(new URL('../src/features/governance-monitor/evidence.json', import.meta.url), 'utf8')));
const digest = (b) => createHash('sha256').update(b).digest('hex');
const encode = (value) => Buffer.from(JSON.stringify(value));
const response = (b = bytes) => new Response(b, { status: 200, headers: { 'Content-Type': 'application/json' } });
const wire = { loopback_requests: 0, methods_rejected: 0, snapshots_rejected: 0, commercial_requests: 0, remote_requests: 0 };
const wireRecords = [];
const scratch = tmpdir();
const [allowedOrigin] = demoConfig(process.env).origins;
async function withServer(work, options = {}) {
  const directory = await mkdtemp(path.join(scratch, 'governance-fixture-'));
  await writeFile(path.join(directory, 'hp-hnp-demo-v1.json'), bytes);
  const { server, counters } = createDemoServer({ fixtureDirectory: directory, ...options });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const port = server.address().port;
  const request = (method = 'GET', pathname = '/snapshots/hp-hnp-demo-v1', headers = {}) => new Promise((resolve, reject) => {
    // Every actual transport request in this suite is confined to this local listener.
    wire.loopback_requests++;
    if (/\/(?:checkout|purchases|assistant|complete|orders|payments|lab)(?:\/|_|$)/.test(pathname)) wire.commercial_requests++;
    const req = http.request({ host: '127.0.0.1', port, method, path: pathname, headers }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        wireRecords.push({ host: '127.0.0.1', port, target: 'test_fixture_server', method, path: pathname, status: res.statusCode });
        resolve({ status: res.statusCode, headers: res.headers, bytes: Buffer.concat(chunks) });
      });
    });
    req.on('error', reject); req.end();
  });
  try { await work({ request, counters, directory }); }
  finally {
    wire.methods_rejected += counters.methods_rejected;
    wire.snapshots_rejected += counters.snapshots_rejected;
    await new Promise((resolve) => server.close(resolve));
    const relative = path.relative(scratch, directory);
    assert.ok(relative.startsWith('governance-fixture-') && !relative.includes(path.sep) && !path.isAbsolute(relative));
    await rm(directory, { recursive: true });
  }
}
const render = (state) => renderToStaticMarkup(createElement(SourceView, { state, onOffline() {}, onLocal() {} }));

test('snapshot preparation is deterministic from the unchanged closed source and matches its pin', async () => {
  assert.deepEqual(await preparedFixtureBytes(), bytes);
  assert.equal(digest(bytes), DEMO_SNAPSHOT_SHA256);
});

test('HTTP GET serves exactly the pinned four-scenario offline bundle, no new events', async () => {
  assert.deepEqual(snapshot.bundle, offline);
  await withServer(async ({ request, counters }) => {
    const r = await request('GET', undefined, { Origin: allowedOrigin });
    assert.equal(r.status, 200);
    assert.equal(digest(r.bytes), digest(bytes));
    assert.equal(r.headers['access-control-allow-origin'], allowedOrigin);
    assert.equal(counters.served, 1);
    assert.equal(parseDemoSnapshot(JSON.parse(r.bytes)).liveEvents, false);
  });
});
test('demo settings keep the reviewed defaults and are overridable only by the environment', () => {
  const defaults = demoConfig({});
  assert.deepEqual(defaults, { port: 5183, snapshotUrl: 'http://127.0.0.1:5183/snapshots/hp-hnp-demo-v1', origins: ['http://127.0.0.1:5182', 'http://127.0.0.1:5179'], directory: '.local/mon-02b-2026-10-18' });
  assert.equal(DEMO_SNAPSHOT_URL, defaults.snapshotUrl);
  const custom = demoConfig({ VITE_GOVERNANCE_SNAPSHOT_PORT: '6200', GOVERNANCE_SNAPSHOT_ORIGINS: ' http://localhost:5173 ,', GOVERNANCE_DEMO_DIR: '.local/other-run' });
  assert.deepEqual(custom, { port: 6200, snapshotUrl: 'http://127.0.0.1:6200/snapshots/hp-hnp-demo-v1', origins: ['http://localhost:5173'], directory: '.local/other-run' });
  for (const env of [{ VITE_GOVERNANCE_SNAPSHOT_PORT: '0' }, { VITE_GOVERNANCE_SNAPSHOT_PORT: '5183abc' }, { GOVERNANCE_SNAPSHOT_ORIGINS: 'https://example.invalid' }, { GOVERNANCE_SNAPSHOT_ORIGINS: 'http://127.0.0.1:5182/path' }]) {
    assert.throws(() => demoConfig(env), /inválido/, JSON.stringify(env));
  }
});
test('HTTP CORS follows the configured loopback origins only', async () => {
  const { origins } = demoConfig({ GOVERNANCE_SNAPSHOT_ORIGINS: 'http://localhost:5173' });
  await withServer(async ({ request }) => {
    const r = await request('GET', undefined, { Origin: 'http://localhost:5173' });
    assert.equal(r.status, 200);
    assert.equal(r.headers['access-control-allow-origin'], 'http://localhost:5173');
    assert.equal((await request('GET', undefined, { Origin: 'http://127.0.0.1:5182' })).status, 403);
  }, { origins });
});
test('HTTP unknown ID, traversal, encoded paths and query strings are rejected', async () => {
  await withServer(async ({ request, counters }) => {
    for (const path of ['/snapshots/unknown', '/snapshots/../secret', '/snapshots/%2e%2e/secret', '/snapshots/hp-hnp-demo-v1?path=secret', '/snapshots/hp-hnp-demo-v1/']) assert.equal((await request('GET', path)).status, 404);
    assert.equal(counters.served, 0);
  });
});
test('HTTP POST PUT PATCH DELETE OPTIONS HEAD cannot mutate or read snapshots', async () => {
  await withServer(async ({ request, counters }) => {
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD']) assert.equal((await request(method)).status, 405);
    assert.equal(counters.methods_rejected, 6);
    assert.equal(counters.served, 0);
  });
});
test('HTTP disallowed Origin Host and unsolicited credentials are rejected', async () => {
  await withServer(async ({ request }) => {
    for (const headers of [{ Origin: 'https://example.invalid' }, { Host: 'example.invalid' }, { Authorization: 'Bearer synthetic-test-only' }, { Cookie: 'synthetic-test-only=1' }]) assert.equal((await request('GET', undefined, headers)).status, 403);
  });
});
test('HTTP altered file is detected on every read, without changing original fixture', async () => {
  await withServer(async ({ request, directory }) => {
    assert.equal((await request()).status, 200);
    await writeFile(directory + '/hp-hnp-demo-v1.json', Buffer.concat([bytes, Buffer.from(' ')]));
    const r = await request();
    assert.equal(r.status, 422);
    assert.equal(JSON.parse(r.bytes).code, 'DEMO_SNAPSHOT_INVALID');
  });
  assert.deepEqual(await preparedFixtureBytes(), bytes);
});
test('HTTP oversized fixture is rejected before serving any snapshot', async () => {
  await withServer(async ({ request, directory }) => {
    await writeFile(directory + '/hp-hnp-demo-v1.json', Buffer.alloc(MAX_SNAPSHOT_BYTES + 1));
    assert.equal((await request()).status, 422);
  });
});
test('contract rejects invalid schema after a matching unit-test hash', () => {
  const value = structuredClone(snapshot); value.schema = 'unknown/1'; const b = encode(value);
  assert.throws(() => validateSnapshotBytes(b, digest(b)));
});
test('contract rejects denied execution with effect even with a matching test hash', () => {
  const value = structuredClone(snapshot); value.bundle.scenarios[2].effectOccurred = true; const b = encode(value);
  assert.throws(() => validateSnapshotBytes(b, digest(b)), /inconsistente/);
});
test('contract rejects not-started confirmed creation', () => {
  const value = structuredClone(snapshot); value.bundle.scenarios[0].execution = 'not_started';
  assert.throws(() => parseDemoSnapshot(value), /inconsistente/);
});
test('contract rejects secret-like or personal strings before serving', () => {
  for (const text of ['Bearer synthetic-secret-string', 'fixture@example.invalid']) {
    const value = structuredClone(snapshot); value.bundle.scenarios[0].intent = text;
    assert.throws(() => parseDemoSnapshot(value), /Redacción/);
  }
});
test('production runtime is explicitly disabled', () => {
  const previous = process.env.NODE_ENV;
  try { process.env.NODE_ENV = 'production'; assert.throws(() => assertDevelopmentRuntime(), /DISABLED/); assert.throws(() => createDemoServer()); }
  finally { if (previous === undefined) delete process.env.NODE_ENV; else process.env.NODE_ENV = previous; }
});
test('client sends only GET to the fixed local ID and omits credentials', async () => {
  const calls = [];
  const value = await loadLocalSnapshot({ fetchImpl: async (url, init) => { calls.push({ url, method: init.method, credentials: init.credentials, redirect: init.redirect }); return response(); } });
  assert.deepEqual(value.bundle, offline);
  assert.deepEqual(calls, [{ url: DEMO_SNAPSHOT_URL, method: 'GET', credentials: 'omit', redirect: 'error' }]);
});
test('client verifies actual HTTP bytes through the local test listener', async () => {
  await withServer(async ({ request }) => {
    const result = await loadLocalSnapshot({ fetchImpl: async (url, init) => {
      assert.equal(url, DEMO_SNAPSHOT_URL); assert.equal(init.method, 'GET');
      const r = await request(); return new Response(r.bytes, { status: r.status, headers: r.headers });
    } });
    assert.deepEqual(result, snapshot);
  });
});
test('client rejects altered bytes rather than trusting a response header', async () => {
  await assert.rejects(loadLocalSnapshot({ fetchImpl: async () => response(Buffer.concat([bytes, Buffer.from(' ')])) }), (e) => e.kind === 'inconsistent');
});
test('client rejects HTML and HTTP invalid-snapshot responses', async () => {
  await assert.rejects(loadLocalSnapshot({ fetchImpl: async () => new Response('<html/>') }), (e) => e.kind === 'inconsistent');
  await assert.rejects(loadLocalSnapshot({ fetchImpl: async () => new Response('{}', { status: 422 }) }), (e) => e.kind === 'inconsistent');
});
test('client timeout is bounded even if a transport double ignores abort', async () => {
  await assert.rejects(loadLocalSnapshot({ fetchImpl: () => new Promise(() => {}), timeoutMs: 10 }), (e) => e.kind === 'timeout');
});
test('client timeout includes a stalled response body, not only HTTP headers', async () => {
  await assert.rejects(loadLocalSnapshot({ fetchImpl: async () => new Response(new ReadableStream({ start() {} }), { headers: { 'Content-Type': 'application/json' } }), timeoutMs: 10 }), (e) => e.kind === 'timeout');
});
test('client reports disconnection without inventing evidence', async () => {
  await assert.rejects(loadLocalSnapshot({ fetchImpl: async () => { throw Error('offline'); } }), (e) => e.kind === 'disconnected');
});
test('client caller cancellation aborts an in-flight read', async () => {
  const controller = new AbortController();
  const pending = loadLocalSnapshot({ signal: controller.signal, fetchImpl: () => new Promise(() => {}) });
  controller.abort();
  await assert.rejects(pending, (e) => e.kind === 'disconnected');
});
test('client rejects excessive response size', async () => {
  await assert.rejects(loadLocalSnapshot({ fetchImpl: async () => response(Buffer.alloc(MAX_SNAPSHOT_BYTES + 1)) }), (e) => e.kind === 'inconsistent');
});
test('failure before first GET success never silently substitutes the offline bundle', () => {
  const state = rejectLocalRead(beginLocalRead(offlineSource(offline)), new SnapshotReadError('disconnected'));
  assert.equal(state.mode, 'local'); assert.equal(state.status, 'disconnected'); assert.equal(state.bundle, null);
  const html = render(state);
  assert.ok(html.includes('Fuente desconectada')); assert.ok(html.includes('Reproducción offline'));
  assert.ok(!html.includes('Detalle del escenario'));
});
test('failed reread marks the last complete snapshot stale, without mixing fields', () => {
  const success = acceptLocalRead(snapshot, 'TEST_READ_TIME_NOT_EVENT_TIME');
  const stale = rejectLocalRead(success, new SnapshotReadError('timeout'));
  assert.equal(stale.status, 'stale'); assert.equal(stale.bundle, snapshot.bundle);
  assert.equal(stale.readAt, success.readAt); assert.ok(render(stale).includes('última lectura obsoleta'));
});
test('inconsistent new snapshot hides all prior evidence from that source', () => {
  const state = rejectLocalRead(acceptLocalRead(snapshot, 'TEST_READ_TIME'), new SnapshotReadError('inconsistent'));
  assert.equal(state.status, 'inconsistent'); assert.equal(state.bundle, null);
  assert.ok(render(state).includes('snapshot rechazado íntegramente'));
});
test('explicit offline selection restores exactly the original bundle', () => {
  const state = offlineSource(offline);
  assert.equal(state.bundle, offline); assert.equal(state.mode, 'offline'); assert.equal(state.readAt, null);
  assert.ok(render(state).includes('sin lectura HTTP'));
});
test('both modes preserve limits and identify historical demo snapshots, never live events', () => {
  for (const state of [offlineSource(offline), acceptLocalRead(snapshot, 'TEST_READ_TIME_NOT_EVENT_TIME')]) {
    const html = render(state);
    for (const label of ['Reproducción offline', 'Snapshot backend local', 'PARTIAL', 'I10/D08/D09', 'AUDIT-MERCHANT-CANCEL-01', 'Sin autenticación real']) assert.ok(html.includes(label), label);
  }
  const html = render(acceptLocalRead(snapshot, 'TEST_READ_TIME_NOT_EVENT_TIME'));
  assert.ok(html.includes('fixture histórica/obsoleta')); assert.ok(html.includes('no es un evento en vivo'));
  assert.ok(html.includes('no tiempo del evento'));
});
after(() => {
  assert.equal(wire.commercial_requests, 0); assert.equal(wire.remote_requests, 0);
  assert.equal(wireRecords.length, wire.loopback_requests);
  assert.ok(wireRecords.every((r) => r.host === '127.0.0.1' && r.target === 'test_fixture_server'));
  // Printed rather than written under `.local`, so a clean checkout stays clean.
  console.log(`HTTP_TRANSPORT_REPORT=${JSON.stringify({ ...wire, scope: 'all actual HTTP transports in this test file; not a host-wide packet capture', records: wireRecords })}`);
});
