import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, realpath, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import test from 'node:test';
import { standDown } from './stand-down.mjs';
import { readTeamReceipt } from './receipt-contract.js';

const exec = promisify(execFile);
const hash = data => createHash('sha256').update(data).digest('hex');
const put = (path, content) => writeFile(path, JSON.stringify(content) + '\n');
const get = async path => JSON.parse(await readFile(path, 'utf8'));
const digest = async path => hash(await readFile(path));
const handoff = { worked: 'Compared exact log.', wentWrong: 'No mistake observed.', carryForward: 'Retain a pinned log.' };
const entry = fileURLToPath(new URL('./stand-down.mjs', import.meta.url));

async function fixture(t, version = 2) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'stand-down-fixture-')));
  t.after(() => rm(root, { force: true, recursive: true }));
  const project = join(root, 'project');
  const receiptStore = join(root, 'receipts');
  const receiptDir = join(receiptStore, hash(project));
  await mkdir(project);
  await mkdir(receiptDir, { recursive: true });
  const assignmentId = '5'.repeat(32);
  const terminalId = 't101';
  const tabId = 'tb' + 'a'.repeat(32);
  const controller = { role: 'Chief of Staff', sessionId: 'controller' };
  const assignment = { assignmentId, project, role: 'Engineering', sessionId: 'worker', terminalId,
    createdMs: 1700000000000, tabId, task: 'Verify synthetic work', state: 'completed' };
  const record = { project, controller, ownerHold: false, paused: false, assignments: [assignment] };
  const opts = { assignment: assignmentId, registry: join(root, 'registry.json'),
    review: join(root, 'review.json'), receiptStore };
  const persist = join(root, 'acceptance');
  const acceptedPath = join(persist, assignmentId + '.accepted.json');
  const intentPath = join(persist, assignmentId + '.settlement-intent.json');
  const receiptPath = join(receiptDir, assignmentId + '.json');
  const artifactPath = join(root, 'log');
  await writeFile(artifactPath, 'synthetic test log\n');
  const { role, sessionId, task, createdMs } = assignment;
  const receipt = { version, assignmentId, role, sessionId, project, task, outcome: 'completed',
    recordedAt: new Date(assignment.createdMs + 1000).toISOString(), summary: 'Verified synthetic work',
    checks: [{ command: 'mock-check', exitCode: 0 }], artifacts: [artifactPath] };
  if (version === 2) receipt.learning = handoff;
  await put(opts.registry, record);
  await put(receiptPath, receipt);
  const review = { assignmentId, reviewer: controller, receiptSha256: await digest(receiptPath),
    evidence: [{ path: artifactPath, sha256: await digest(artifactPath) }], limits: 'No real Herdr or deployment coverage.' };
  await put(opts.review, review);
  const observation = { assignmentId, activeAssignmentIds: [], ownerHold: false, paused: false,
    terminal: { id: terminalId, createdMs, state: 'live', activity: 'done', role, sessionId, project },
    tab: { id: tabId, terminalIds: [terminalId], paneCount: 1, settled: false } };
  const calls = [];
  const herdr = {
    async observe() { calls.push('observe'); return structuredClone(observation); },
    async settle(tab) {
      calls.push('settle');
      assert.equal(tab, tabId);
      assert.equal((await get(acceptedPath)).state, 'accepted');
      assert.equal((await get(intentPath)).state, 'pending');
      observation.tab.settled = true;
    },
  };
  return { root, opts, persist, acceptedPath, intentPath, receiptPath, artifactPath, receipt, review, calls,
    assignment, record, observation, herdr };
}

async function resultFile(f, result) {
  return get(join(f.persist, `${f.opts.assignment}.${result.run}.result.json`));
}

test('v2 dry-run is read-only; persist before one reversible settlement and verify confirmation', async t => {
  const f = await fixture(t);
  const original = await readFile(f.receiptPath);
  const dry = await standDown(f.opts, f.herdr);
  assert.deepEqual(dry, { state: 'ready', assignmentId: f.opts.assignment, tabId: f.assignment.tabId });
  assert.deepEqual(f.calls, ['observe']);
  assert.ok(!(await readdir(f.root)).includes('acceptance'));
  assert.equal((await standDown({ ...f.opts, persist: f.persist }, f.herdr)).state, 'accepted');
  assert.ok(!f.calls.includes('settle'));
  const result = await standDown({ ...f.opts, persist: f.persist, settle: true }, f.herdr);
  assert.equal(result.state, 'settled');
  assert.deepEqual(f.calls.slice(-4), ['observe', 'observe', 'settle', 'observe']);
  assert.deepEqual(await resultFile(f, result), result);
  const accepted = await get(f.acceptedPath);
  assert.deepEqual(accepted.handoff, { source: 'v2_receipt', learning: handoff });
  assert.equal(accepted.identity.role, 'Engineering');
  assert.equal(accepted.limits, f.review.limits);
  assert.deepEqual(await readFile(f.receiptPath), original);
  assert.equal((await readdir(f.persist)).filter(name => name.endsWith('.lock')).length, 0);
});

test('legacy v1 remains readable and unchanged; separately approved attributed supplement is required', async t => {
  const f = await fixture(t, 1);
  const old = await readFile(f.receiptPath);
  assert.equal((await readTeamReceipt(f.assignment.project, f.assignment.assignmentId, f.opts.receiptStore)).receipt.version, 1);
  const request = { ...f.opts, persist: f.persist, settle: true };
  const unsupported = await standDown(request, f.herdr);
  assert.equal(unsupported.state, 'incomplete');
  assert.equal(unsupported.reason, 'legacy_supplement_required');
  assert.equal((await resultFile(f, unsupported)).reason, unsupported.reason);
  assert.equal(f.calls.length, 0);
  await assert.rejects(readFile(f.acceptedPath), { code: 'ENOENT' });
  const { assignmentId, project, role, sessionId } = f.assignment;
  const supplement = join(f.root, 'supplement.json');
  const entry = { version: 1, assignmentId, project, role, sessionId,
    receiptSha256: hash(old), author: { role: 'Chief of Staff', sessionId: 'controller' },
    basis: 'Attributed from the retained worker handoff; no generated claims.', learning: handoff };
  await put(supplement, { ...entry, receiptSha256: 'e'.repeat(64) });
  assert.equal((await standDown({ ...request, supplement }, f.herdr)).reason, 'supplement_identity_mismatch');
  await put(supplement, entry);
  await put(f.opts.review, { ...f.review, supplementSha256: await digest(supplement) });
  f.observation.terminal.activity = 'idle';
  assert.equal((await standDown({ ...request, supplement }, f.herdr)).state, 'settled');
  assert.deepEqual(await readFile(f.receiptPath), old);
  const accepted = await get(f.acceptedPath);
  assert.equal(accepted.handoff.source, 'v1_supplement');
  assert.deepEqual(accepted.handoff.author, entry.author);
  assert.deepEqual(accepted.handoff.learning, handoff);
  assert.equal(accepted.identity.role, 'Engineering');
});

test('unregistered, foreign, stale, missing, active, held or incomplete paths never settle', async t => {
  const cases = [
    ['missing registration', f => put(f.opts.registry, { ...f.record, assignments: [] }), 'assignment_not_registered'],
    ['conflicting work', f => put(f.opts.registry, { ...f.record, assignments: [f.assignment,
      { ...f.assignment, assignmentId: '6'.repeat(32), state: 'working' }] }), 'conflicting_assignment'],
    ['interrupted assignment', f => put(f.opts.registry, { ...f.record, assignments: [{ ...f.assignment, state: 'interrupted' }] }), 'assignment_incomplete'],
    ['missing receipt', f => rm(f.receiptPath), 'receipt_unreadable'],
    ['foreign role', f => put(f.receiptPath, { ...f.receipt, role: 'Watchdog' }), 'receipt_identity_mismatch'],
    ['foreign session', f => put(f.receiptPath, { ...f.receipt, sessionId: 'another' }), 'receipt_identity_mismatch'],
    ['foreign project', f => put(f.receiptPath, { ...f.receipt, project: f.root }), 'receipt_identity_mismatch'],
    ['foreign assignment', f => put(f.receiptPath, { ...f.receipt, assignmentId: '7'.repeat(32) }), 'receipt_identity_mismatch'],
    ['failed outcome', f => put(f.receiptPath, { ...f.receipt, outcome: 'failed' }), 'receipt_incomplete'],
    ['stale receipt from old generation', f => put(f.receiptPath, { ...f.receipt,
      recordedAt: new Date(f.assignment.createdMs - 1).toISOString() }), 'receipt_time_mismatch'],
    ['missing learning', f => put(f.receiptPath, { ...f.receipt, learning: { ...handoff, wentWrong: ' ' } }), 'learning_missing'],
    ['missing limits', f => put(f.opts.review, { ...f.review, limits: ' ' }), 'review_mismatch'],
    ['foreign review', f => put(f.opts.review, { ...f.review, reviewer: f.receipt }), 'review_mismatch'],
    ['missing evidence file', f => rm(f.artifactPath), 'stale_evidence'],
    ['tampered evidence file', f => writeFile(f.artifactPath, 'different'), 'stale_evidence'],
    ['stale terminal generation', f => { f.observation.terminal.createdMs += 1; }, 'terminal_not_settled_for_assignment'],
    ['different Pi on same generation', f => { f.observation.terminal.sessionId = 'different'; }, 'terminal_not_settled_for_assignment'],
    ['working terminal', f => { f.observation.terminal.activity = 'working'; }, 'terminal_not_settled_for_assignment'],
    ['wrong tab', f => { f.observation.tab.id = 'tb' + 'b'.repeat(32); }, 'tab_membership_or_state_mismatch'],
    ['unrelated pane in same tab', f => { f.observation.tab.terminalIds.push('t-other'); }, 'tab_membership_or_state_mismatch'],
    ['nonterminal pane in same tab', f => { f.observation.tab.paneCount = 2; }, 'tab_membership_or_state_mismatch'],
    ['already settled elsewhere', f => { f.observation.tab.settled = true; }, 'tab_membership_or_state_mismatch'],
    ['active observed assignment', f => { f.observation.activeAssignmentIds.push('7'.repeat(32)); }, 'conflicting_or_unobserved_assignment'],
    ['owner hold', f => put(f.opts.registry, { ...f.record, ownerHold: true }), 'held_or_paused'],
    ['observed pause', f => { f.observation.paused = true; }, 'held_or_unobserved'],
    ['unknown observed hold', f => { delete f.observation.ownerHold; }, 'held_or_unobserved'],
  ];
  for (const [name, modify, expected] of cases) await t.test(name, async t => {
    const f = await fixture(t);
    await modify(f);
    const result = await standDown({ ...f.opts, persist: f.persist, settle: true }, f.herdr);
    assert.equal(result.state, 'incomplete');
    assert.equal(result.reason, expected);
    assert.deepEqual(await resultFile(f, result), result);
    await assert.rejects(readFile(f.acceptedPath), { code: 'ENOENT' });
    assert.ok(!f.calls.includes('settle'));
  });
});

test('fresh hold during persist-to-settle gap preserves acceptance and incomplete result without calling Desktop', async t => {
  const f = await fixture(t);
  let checks = 0;
  const result = await standDown({ ...f.opts, persist: f.persist, settle: true }, {
    ...f.herdr,
    async observe() {
      if (++checks === 2) f.observation.ownerHold = true;
      return f.herdr.observe();
    },
  });
  assert.equal(result.state, 'incomplete');
  assert.equal(result.stage, 'recheck');
  assert.equal((await get(f.acceptedPath)).state, 'accepted');
  assert.deepEqual(await resultFile(f, result), result);
  assert.ok(!f.calls.includes('settle'));
});

test('interrupted or unconfirmed settlement is retained incomplete and never retried', async t => {
  for (const failure of ['abort', 'no-confirmation', 'transport-error']) await t.test(failure, async t => {
    const f = await fixture(t);
    const controller = new AbortController();
    const request = { ...f.opts, persist: f.persist, settle: true };
    const result = await standDown({ ...request, signal: controller.signal }, {
      ...f.herdr,
      async settle() {
        f.calls.push('settle');
        if (failure === 'abort') controller.abort();
        if (failure === 'transport-error') throw new Error('No acknowledgement');
      },
    });
    assert.equal(result.state, 'incomplete');
    if (failure === 'abort') assert.equal(result.reason, 'interrupted');
    assert.deepEqual(await resultFile(f, result), result);
    assert.equal((await get(f.intentPath)).state, 'pending');
    assert.equal((await standDown(request, f.herdr)).reason, 'settlement_intent_already_exists');
    assert.equal(f.calls.filter(call => call === 'settle').length, 1);
    assert.equal(f.observation.tab.settled, false);
  });
});

test('stand-down persistence serializes competing CoS invocations in the same evidence directory', async t => {
  const f = await fixture(t);
  let arrived, release;
  const pending = new Promise(resolve => { arrived = resolve; });
  const continueCheck = new Promise(resolve => { release = resolve; });
  const request = { ...f.opts, persist: f.persist, settle: true };
  const first = standDown(request, { ...f.herdr, async observe() {
    arrived();
    await continueCheck;
    return f.herdr.observe();
  } });
  await pending;
  try { assert.equal((await standDown(request, f.herdr)).reason, 'stand_down_already_in_progress'); }
  finally { release(); }
  assert.equal((await first).state, 'settled');
  assert.equal(f.calls.filter(call => call === 'settle').length, 1);
});

test('actual CLI uses only fake Herdr, exact tab.settle command, and explicit persistence', async t => {
  const f = await fixture(t);
  const source = join(f.root, 'snapshot.json');
  const observer = join(f.root, 'observe.mjs');
  const desktop = join(f.root, 'desktop.mjs');
  await put(source, f.observation);
  const prelude = `#!${process.execPath}\nimport assert from 'node:assert/strict';\nimport {readFileSync, writeFileSync} from 'node:fs';\n`;
  await writeFile(observer, prelude + `assert.deepEqual(process.argv.slice(2), ${JSON.stringify([f.assignment.assignmentId, f.assignment.terminalId, f.assignment.tabId])});\nprocess.stdout.write(readFileSync(${JSON.stringify(source)}));\n`, { mode: 0o700 });
  await writeFile(desktop, prelude + `assert.deepEqual(process.argv.slice(2), ${JSON.stringify(['do', 'tab.settle', '--id', f.assignment.tabId])});\nassert.equal(JSON.parse(readFileSync(${JSON.stringify(f.acceptedPath)})).state, 'accepted');\nassert.equal(JSON.parse(readFileSync(${JSON.stringify(f.intentPath)})).state, 'pending');\nconst data = JSON.parse(readFileSync(${JSON.stringify(source)}));\ndata.tab.settled = true;\nwriteFileSync(${JSON.stringify(source)}, JSON.stringify(data));\n`, { mode: 0o700 });
  const args = [entry, '--assignment', f.opts.assignment, '--registry', f.opts.registry, '--review', f.opts.review,
    '--receipt-store', f.opts.receiptStore, '--observer', observer, '--desktop', desktop];
  const invoke = (...argv) => exec(process.execPath, [...args, ...argv], { env: {}, timeout: 5000 });
  await assert.rejects(invoke('--settle'), { code: 2 });
  assert.equal(JSON.parse((await invoke('--dry-run')).stdout).state, 'ready');
  assert.equal((await get(source)).tab.settled, false);
  assert.equal(JSON.parse((await invoke('--persist', f.persist, '--settle')).stdout).state, 'settled');
  assert.equal((await get(source)).tab.settled, true);
});
