/** Local governance monitor demo: fixture snapshot, Better Auth and the audit preview; Ctrl+C stops all three. */
import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, rmSync } from 'node:fs';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { DEMO_HOST, demoConfig } from '../ui/src/features/governance-monitor/demo-config.ts';
import { DEMO_SNAPSHOT_SHA256 } from '../ui/src/features/governance-monitor/snapshot-pin.ts';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const VITE = path.join(ROOT, 'ui/node_modules/vite/bin/vite.js');
// Fixed by ui/governance-preview.config.mjs (strictPort) and by the proxy default in ui/vite.config.ts.
const PREVIEW_ORIGIN = `http://${DEMO_HOST}:5182`;
const AUTH_PORT = 4210;

function fail(message) {
  console.error(`[demo] ${message}`);
  process.exit(1);
}

function runNode(name, args, options) {
  const child = spawn(process.execPath, args, { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], ...options });
  for (const stream of [child.stdout, child.stderr]) createInterface({ input: stream }).on('line', (line) => console.log(`[${name}] ${line}`));
  return child;
}

async function waitFor(url, ready = (response) => response.ok) {
  for (let attempt = 0; attempt < 150 && !stopping; attempt++) {
    try { if (ready(await fetch(url))) return; } catch { /* still starting */ }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw Error(`${url} did not answer`);
}

if (process.env.NODE_ENV === 'production') fail('The demo is disabled in production.');
const config = demoConfig(process.env);
const directory = path.resolve(ROOT, config.directory);
// An explicit upstream means the caller runs Better Auth; the preview proxies /api/auth there.
const externalAuth = process.env.AUTH_DEV_UPSTREAM;
if (!existsSync(VITE)) fail('Run `npm ci` in ui/ first.');
if (!externalAuth && !existsSync(path.join(ROOT, 'auth/node_modules/better-auth'))) fail('Run `npm ci` in auth/ first, or set AUTH_DEV_UPSTREAM.');

const step = (args, env) => spawnSync(process.execPath, args, { cwd: ROOT, stdio: 'inherit', env: { ...process.env, ...env } }).status === 0;
if (!step(['scripts/prepare_governance_snapshot.mjs', '--demo-fixtures-only'])) {
  fail(`Fixture preparation failed. A fixture from an older bundle is never overwritten; if that is the cause, delete ${path.join(directory, 'fixtures')} and retry.`);
}
// The flag is read at build time; a separate outDir keeps ui/dist as the default, monitor-free build.
const outDir = path.join(directory, 'ui-dist');
console.log('[demo] Building the UI with VITE_GOVERNANCE_MONITOR=true');
if (!step([VITE, 'build', 'ui', '--outDir', outDir, '--emptyOutDir', '--logLevel', 'warn'], { VITE_GOVERNANCE_MONITOR: 'true' })) fail('UI build failed.');

const children = [];
let stopping = false;
function stop(code) {
  if (stopping) return;
  stopping = true;
  process.exitCode = code;
  for (const child of children) if (child.exitCode === null && child.signalCode === null) child.kill();
  setTimeout(() => children.forEach((child) => child.kill('SIGKILL')), 5000).unref();
}
function start(name, args, options) {
  const child = runNode(name, args, options);
  child.once('exit', (code, signal) => {
    if (!stopping) { console.error(`[demo] ${name} exited (${signal ?? code}); stopping the demo.`); stop(1); }
  });
  children.push(child);
}
process.once('SIGINT', () => stop(0));
process.once('SIGTERM', () => stop(0));

start('snapshot', ['scripts/governance_snapshot_server.mjs', '--demo-fixtures-only']);
if (!externalAuth) {
  // Synthetic accounts only: an ephemeral secret and a fresh database on every launch, as in run_joint_e2e.py.
  const database = path.join(directory, 'auth.db');
  mkdirSync(directory, { recursive: true });
  rmSync(database, { force: true });
  start('auth', ['server.js'], {
    cwd: path.join(ROOT, 'auth'),
    env: { ...process.env, BETTER_AUTH_URL: PREVIEW_ORIGIN, BETTER_AUTH_SECRET: randomBytes(32).toString('base64url'), AUTH_DATABASE_PATH: database, PORT: String(AUTH_PORT) },
  });
}
start('ui', [VITE, 'preview', 'ui', '--config', 'ui/governance-preview.config.mjs', '--outDir', outDir], { env: { ...process.env, VITE_GOVERNANCE_MONITOR: 'true' } });

try {
  await waitFor(config.snapshotUrl, (response) => response.status === 200 && response.headers.get('x-demo-snapshot-sha256') === DEMO_SNAPSHOT_SHA256);
  await waitFor(externalAuth ? `${PREVIEW_ORIGIN}/api/auth/jwks` : `http://${DEMO_HOST}:${AUTH_PORT}/api/auth/jwks`);
  await waitFor(`${PREVIEW_ORIGIN}/governance`);
} catch (error) {
  if (!stopping) { console.error(`[demo] ${error.message}; stopping the demo.`); stop(1); }
}
if (!stopping) {
  console.log([
    `[demo] Ready. Sign up with a synthetic email and password${externalAuth ? '' : '; local accounts last until Ctrl+C'}.`,
    `[demo]   Monitor   ${PREVIEW_ORIGIN}/governance`,
    `[demo]   Snapshot  ${config.snapshotUrl} (sha256 ${DEMO_SNAPSHOT_SHA256})`,
    `[demo]   Auth      ${externalAuth ?? `http://${DEMO_HOST}:${AUTH_PORT}`}, proxied at ${PREVIEW_ORIGIN}/api/auth`,
    '[demo] Ctrl+C stops every process.',
  ].join('\n'));
}
