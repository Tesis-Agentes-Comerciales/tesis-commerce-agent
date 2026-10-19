/** Development fixture reader only. No application imports, DBs, HTTP clients or SDKs. */
import http from 'node:http';
import { createHash } from 'node:crypto';
import { lstat, realpath, open } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseDemoSnapshot, MAX_SNAPSHOT_BYTES, DEMO_SNAPSHOT_ID } from '../ui/src/features/governance-monitor/snapshot-contract.ts';
import { DEMO_SNAPSHOT_SHA256 } from '../ui/src/features/governance-monitor/snapshot-pin.ts';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FIXTURES = path.join(ROOT, '.local/mon-02b-2026-10-18/fixtures');
const CATALOG = Object.freeze({ [DEMO_SNAPSHOT_ID]: Object.freeze({ file: 'hp-hnp-demo-v1.json', sha256: DEMO_SNAPSHOT_SHA256 }) });
const ORIGINS = new Set(['http://127.0.0.1:5182', 'http://127.0.0.1:5179']);

export function assertDevelopmentRuntime() {
  if (process.env.NODE_ENV === 'production') throw Error('DEMO_DISABLED_IN_PRODUCTION');
}

export function validateSnapshotBytes(bytes, expectedHash = DEMO_SNAPSHOT_SHA256) {
  if (bytes.byteLength > MAX_SNAPSHOT_BYTES) throw Error('SNAPSHOT_TOO_LARGE');
  if (createHash('sha256').update(bytes).digest('hex') !== expectedHash) throw Error('SNAPSHOT_HASH_MISMATCH');
  parseDemoSnapshot(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)));
  return bytes;
}

export async function readAuthorizedSnapshot(directory = FIXTURES) {
  const item = CATALOG[DEMO_SNAPSHOT_ID];
  if ((await lstat(directory)).isSymbolicLink()) throw Error('FIXTURE_LINK_FORBIDDEN');
  const root = await realpath(directory);
  const filename = path.join(root, item.file);
  const info = await lstat(filename);
  if (!info.isFile() || info.isSymbolicLink() || info.size > MAX_SNAPSHOT_BYTES) throw Error('FIXTURE_INVALID');
  if (path.dirname(await realpath(filename)) !== root) throw Error('FIXTURE_OUTSIDE_ROOT');
  const handle = await open(filename, 'r');
  try {
    const buffer = Buffer.alloc(MAX_SNAPSHOT_BYTES + 1);
    let size = 0;
    while (size < buffer.length) {
      const read = await handle.read(buffer, size, buffer.length - size, null);
      if (!read.bytesRead) break;
      size += read.bytesRead;
    }
    return validateSnapshotBytes(buffer.subarray(0, size), item.sha256);
  } finally { await handle.close(); }
}

// directory injection is for isolated fixture-file tests; the CLI has no path argument.
export function createDemoServer({ fixtureDirectory = FIXTURES, audit = () => {} } = {}) {
  assertDevelopmentRuntime();
  const counters = { requests: 0, served: 0, invalid_id_or_path: 0, methods_rejected: 0, origins_rejected: 0, snapshots_rejected: 0 };
  const server = http.createServer(async (req, res) => {
    counters.requests++;
    res.once('finish', () => audit({ event: 'demo_request', method: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'].includes(req.method) ? req.method : 'OTHER', status: res.statusCode, counters: { ...counters } }));
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Vary', 'Origin');
    const origin = req.headers.origin;
    if (origin && ORIGINS.has(origin)) res.setHeader('Access-Control-Allow-Origin', origin);
    const reply = (status, code) => {
      res.writeHead(status, { 'Content-Type': 'application/json', Connection: 'close' });
      res.end(JSON.stringify({ code, classification: 'sanitized_demo_fixture', authenticated: false }));
    };
    if (req.headers.authorization || req.headers.cookie || (origin && !ORIGINS.has(origin)) || req.headers.host !== `127.0.0.1:${server.address().port}`) {
      counters.origins_rejected++; reply(403, 'DEMO_ORIGIN_REJECTED'); return;
    }
    if (req.method !== 'GET') {
      counters.methods_rejected++; res.setHeader('Allow', 'GET'); reply(405, 'GET_ONLY'); return;
    }
    if (req.url !== `/snapshots/${DEMO_SNAPSHOT_ID}`) {
      counters.invalid_id_or_path++; reply(404, 'SNAPSHOT_NOT_AUTHORIZED'); return;
    }
    try {
      const bytes = await readAuthorizedSnapshot(fixtureDirectory);
      counters.served++;
      res.writeHead(200, { 'Content-Type': 'application/json', 'X-Demo-Snapshot-SHA256': DEMO_SNAPSHOT_SHA256 });
      res.end(bytes);
    } catch {
      counters.snapshots_rejected++; reply(422, 'DEMO_SNAPSHOT_INVALID');
    }
  });
  server.maxHeadersCount = 32;
  server.headersTimeout = 2000;
  server.requestTimeout = 3000;
  return { server, counters };
}

async function main() {
  assertDevelopmentRuntime();
  if (process.argv.slice(2).join(' ') !== '--demo-fixtures-only') throw Error('EXPLICIT_DEMO_FLAG_REQUIRED');
  await readAuthorizedSnapshot();
  const { server, counters } = createDemoServer({ audit: (entry) => console.log(JSON.stringify(entry)) });
  server.listen({ host: '127.0.0.1', port: 5183, exclusive: true }, () => {
    console.log(JSON.stringify({ event: 'demo_started', host: '127.0.0.1', port: 5183, snapshot: DEMO_SNAPSHOT_ID, sha256: DEMO_SNAPSHOT_SHA256, authentication: 'none; sanitized demo fixtures only', live_events: false }));
  });
  server.on('error', () => { console.error('DEMO_LISTEN_FAILED'); process.exitCode = 1; });
  const stop = () => server.close(() => { console.log(JSON.stringify({ event: 'demo_stopped', counters })); });
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { console.error('DEMO_START_REJECTED'); process.exitCode = 1; });
}
