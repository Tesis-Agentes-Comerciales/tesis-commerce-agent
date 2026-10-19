/** Deterministic preparation from the already closed offline bundle; never a harness. */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { parseHistoricalEvidence } from '../ui/src/features/governance-monitor/historical.ts';
import { parseDemoSnapshot } from '../ui/src/features/governance-monitor/snapshot-contract.ts';
import { DEMO_SNAPSHOT_SHA256 } from '../ui/src/features/governance-monitor/snapshot-pin.ts';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
export async function preparedFixtureBytes() {
  const raw = await readFile(path.join(root, 'ui/src/features/governance-monitor/evidence.json'));
  if (hash(raw) !== '3b56e16b0708052c0b90beecce0cbd38ce068e6d4df3dfecf1d981f42e981415') throw Error('SOURCE_PIN_MISMATCH');
  const value = parseDemoSnapshot({ schema: 'governance-demo-snapshot/1', snapshotId: 'hp-hnp-demo-v1', classification: 'sanitized_demo_fixture', temporalScope: 'historical_closed_artifacts', liveEvents: false, authenticatedUserEvidence: false, bundle: parseHistoricalEvidence(JSON.parse(raw)) });
  const bytes = Buffer.from(JSON.stringify(value, null, 2) + '\n');
  if (hash(bytes) !== DEMO_SNAPSHOT_SHA256) throw Error('PREPARED_FIXTURE_PIN_MISMATCH');
  return bytes;
}
async function main() {
  if (process.env.NODE_ENV === 'production' || process.argv.slice(2).join(' ') !== '--demo-fixtures-only') throw Error('DEMO_ONLY');
  const bytes = await preparedFixtureBytes();
  const directory = path.join(root, '.local/mon-02b-2026-10-18/fixtures');
  await mkdir(directory, { recursive: true });
  const destination = path.join(directory, 'hp-hnp-demo-v1.json');
  try { await writeFile(destination, bytes, { flag: 'wx' }); }
  catch (error) {
    if (error.code !== 'EEXIST' || hash(await readFile(destination)) !== DEMO_SNAPSHOT_SHA256) throw Error('EXISTING_FIXTURE_NOT_OVERWRITTEN');
  }
  console.log(JSON.stringify({ action: 'prepared_or_verified', sha256: DEMO_SNAPSHOT_SHA256, scenarios: 4, harness_execution: false }));
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(() => { console.error('FIXTURE_PREPARATION_REJECTED'); process.exitCode = 1; });
