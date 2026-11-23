#!/usr/bin/env node
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { open, link, mkdir, readFile, realpath, rmdir, unlink } from 'node:fs/promises';
import { constants } from 'node:fs';
import { isAbsolute, dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual, parseArgs, promisify } from 'node:util';
import { defaultTeamReceiptStore } from './receipt-contract.js';

const exec = promisify(execFile);
const assignmentPattern = /^[a-f0-9]{32}$/;
const tabPattern = /^tb[0-9a-f]{32}$/;
const digestPattern = /^[a-f0-9]{64}$/;
const fields = ['worked', 'wentWrong', 'carryForward'];
const released = new Set(['completed', 'settled', 'cancelled', 'removed']);
const text = value => typeof value === 'string' && !!value.trim();
const matches = (pattern, value) => typeof value === 'string' && pattern.test(value);
const object = value => !!value && typeof value === 'object' && !Array.isArray(value);
const absolute = value => typeof value === 'string' && isAbsolute(value) && !value.includes('\0');
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

class Refusal extends Error {}
function requireThat(condition, code) {
  if (!condition) throw new Refusal(code);
}
function principal(value) {
  return object(value) && text(value.role) && text(value.sessionId);
}
function samePrincipal(left, right) {
  return principal(left) && left.role === right.role && left.sessionId === right.sessionId;
}
function unheld(value) {
  return value.ownerHold === false && value.paused === false;
}
function learning(value) {
  requireThat(object(value) && fields.every(field => text(value[field])), 'learning_missing');
  return Object.fromEntries(fields.map(field => [field, value[field]]));
}

async function readRecord(path, errorCode) {
  try {
    const bytes = await readFile(path);
    const value = JSON.parse(bytes.toString('utf8'));
    requireThat(object(value), errorCode);
    return { value, sha256: sha256(bytes) };
  } catch {
    throw new Refusal(errorCode);
  }
}

async function verifyArtifact(file) {
  requireThat(object(file) && absolute(file.path) && matches(digestPattern, file.sha256), 'invalid_evidence');
  let handle;
  try {
    requireThat(await realpath(file.path) === file.path, 'stale_evidence');
    handle = await open(file.path, constants.O_RDONLY | constants.O_NONBLOCK);
    requireThat((await handle.stat()).isFile(), 'stale_evidence');
    const hash = createHash('sha256');
    let bytes = 0;
    for await (const chunk of handle.createReadStream({ autoClose: false })) {
      hash.update(chunk);
      bytes += chunk.length;
    }
    requireThat(bytes > 0 && hash.digest('hex') === file.sha256, 'stale_evidence');
    return { path: file.path, sha256: file.sha256 };
  } catch {
    throw new Refusal('stale_evidence');
  } finally {
    await handle?.close();
  }
}

function sameAssignment(value, assignment) {
  return value.assignmentId === assignment.assignmentId && value.project === assignment.project &&
    samePrincipal(value, assignment);
}

async function acceptance(opts, observe, phase = 'before') {
  opts.signal?.throwIfAborted();
  const reg = await readRecord(opts.registry, 'registry_unreadable');
  const registry = reg.value;
  requireThat(absolute(registry.project) && await realpath(registry.project) === registry.project &&
    principal(registry.controller) && Array.isArray(registry.assignments), 'invalid_registry');
  requireThat(unheld(registry), 'held_or_paused');
  const assignments = registry.assignments;
  requireThat(assignments.every(row => object(row) && matches(assignmentPattern, row.assignmentId) &&
    principal(row) && absolute(row.project) && text(row.terminalId) && matches(tabPattern, row.tabId) &&
    Number.isSafeInteger(row.createdMs) && row.createdMs > 0 && text(row.state)) &&
    new Set(assignments.map(row => row.assignmentId)).size === assignments.length, 'invalid_registry');
  const bound = assignments.find(row => row.assignmentId === opts.assignment);
  requireThat(bound && bound.project === registry.project && text(bound.task) &&
    bound.sessionId !== registry.controller.sessionId, 'assignment_not_registered');
  requireThat((bound.ownerHold === undefined || bound.ownerHold === false) &&
    (bound.paused === undefined || bound.paused === false), 'held_or_paused');
  requireThat(bound.state === 'completed', 'assignment_incomplete');
  requireThat(!assignments.some(row => row !== bound && !released.has(row.state) &&
    (row.tabId === bound.tabId || row.sessionId === bound.sessionId || row.terminalId === bound.terminalId)),
    'conflicting_assignment');
  const { assignmentId, task, role, sessionId, project, terminalId, createdMs, tabId } = bound;
  const identity = { assignmentId, task, role, sessionId, project, terminalId, createdMs, tabId };
  const receiptPath = join(opts.receiptStore ?? defaultTeamReceiptStore, sha256(project), assignmentId + '.json');
  const input = await readRecord(receiptPath, 'receipt_unreadable');
  const receipt = input.value;
  requireThat(sameAssignment(receipt, identity) && receipt.task === task && [1, 2].includes(receipt.version),
    'receipt_identity_mismatch');
  requireThat(receipt.outcome === 'completed', 'receipt_incomplete');
  const recordedAt = text(receipt.recordedAt) ? Date.parse(receipt.recordedAt) : NaN;
  requireThat(recordedAt >= createdMs && recordedAt <= Date.now(), 'receipt_time_mismatch');
  requireThat(text(receipt.summary) && Array.isArray(receipt.checks) && receipt.checks.every(check =>
    object(check) && text(check.command) && Number.isSafeInteger(check.exitCode)) &&
    Array.isArray(receipt.artifacts) && receipt.artifacts.every(absolute), 'invalid_receipt');
  let handoff;
  if (receipt.version === 1) {
    const supplement = await readRecord(opts.supplement, 'legacy_supplement_required');
    const entry = supplement.value;
    requireThat(entry.version === 1 && sameAssignment(entry, identity) && entry.receiptSha256 === input.sha256 &&
      (samePrincipal(entry.author, identity) || samePrincipal(entry.author, registry.controller)) && text(entry.basis),
      'supplement_identity_mismatch');
    handoff = { source: 'v1_supplement', sha256: supplement.sha256,
      author: { role: entry.author.role, sessionId: entry.author.sessionId }, basis: entry.basis,
      learning: learning(entry.learning) };
  } else {
    requireThat(opts.supplement === undefined, 'unexpected_supplement');
    handoff = { source: 'v2_receipt', learning: learning(receipt.learning) };
  }
  const review = await readRecord(opts.review, 'review_unreadable');
  const approved = review.value;
  requireThat(approved.assignmentId === assignmentId && approved.receiptSha256 === input.sha256 &&
    samePrincipal(approved.reviewer, registry.controller) && approved.supplementSha256 === handoff.sha256 &&
    text(approved.limits) && Array.isArray(approved.evidence) && approved.evidence.length > 0, 'review_mismatch');
  const evidence = [];
  for (const file of approved.evidence) evidence.push(await verifyArtifact(file));
  const paths = new Set(evidence.map(file => file.path));
  requireThat(paths.size === evidence.length && receipt.artifacts.every(path => paths.has(path)), 'incomplete_evidence');
  opts.signal?.throwIfAborted();
  // Get live state after filesystem reads, close to the requested action.
  const current = await observe(identity, opts.signal);
  requireThat(object(current) && unheld(current), 'held_or_unobserved');
  requireThat(current.assignmentId === assignmentId && Array.isArray(current.activeAssignmentIds) &&
    current.activeAssignmentIds.length === 0, 'conflicting_or_unobserved_assignment');
  const { terminal, tab } = current;
  requireThat(object(terminal) && terminal.id === terminalId && terminal.createdMs === createdMs &&
    terminal.project === project && samePrincipal(terminal, identity) && terminal.state === 'live' &&
    ['done', 'idle'].includes(terminal.activity), 'terminal_not_settled_for_assignment');
  requireThat(object(tab) && tab.id === tabId && tab.paneCount === 1 &&
    Array.isArray(tab.terminalIds) && tab.terminalIds.length === 1 &&
    tab.terminalIds[0] === terminalId && tab.settled === (phase === 'after'), 'tab_membership_or_state_mismatch');
  opts.signal?.throwIfAborted();
  return { state: 'accepted', identity, registrySha256: reg.sha256,
    receipt: { version: receipt.version, path: receiptPath, sha256: input.sha256 },
    reviewer: { role: approved.reviewer.role, sessionId: approved.reviewer.sessionId }, reviewSha256: review.sha256,
    handoff, evidence, limits: approved.limits };
}

async function createRecord(path, value) {
  const temporary = join(dirname(path), '.' + randomUUID() + '.tmp');
  const file = await open(temporary, 'wx', 0o600);
  try {
    try {
      await file.writeFile(JSON.stringify(value, null, 2) + '\n', 'utf8');
      await file.sync();
    } finally {
      await file.close();
    }
    await link(temporary, path);
    const directory = await open(dirname(path), 'r');
    try { await directory.sync(); } finally { await directory.close(); }
  } finally {
    await unlink(temporary);
  }
}

export async function standDown(opts, { observe, settle }) {
  requireThat(matches(assignmentPattern, opts.assignment) && (!opts.settle || opts.persist) &&
    typeof observe === 'function' && (!opts.settle || typeof settle === 'function'), 'invalid_request');
  const id = opts.assignment;
  const run = randomUUID();
  let lock, resultFile, stage = 'preflight';
  try {
    let directory;
    if (opts.persist !== undefined) {
      await mkdir(opts.persist, { recursive: true, mode: 0o700 });
      directory = await realpath(opts.persist);
      const candidate = join(directory, id + '.lock');
      await mkdir(candidate, { mode: 0o700 });
      lock = candidate;
      resultFile = join(directory, `${id}.${run}.result.json`);
      await createRecord(join(directory, `${id}.${run}.started.json`),
        { state: 'incomplete', stage: 'started', reason: 'no_result_yet', assignmentId: id, run });
    }
    const approved = await acceptance(opts, observe);
    if (directory === undefined) return { state: 'ready', assignmentId: id, tabId: approved.identity.tabId };
    stage = 'acceptance';
    const acceptedFile = join(directory, id + '.accepted.json');
    try {
      await createRecord(acceptedFile, approved);
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const previous = await readRecord(acceptedFile, 'previous_acceptance_unreadable');
      requireThat(isDeepStrictEqual(previous.value, approved), 'previous_acceptance_differs');
    }
    let state = 'accepted';
    if (opts.settle) {
      stage = 'intent';
      const intent = join(directory, id + '.settlement-intent.json');
      try {
        await createRecord(intent, { state: 'pending', assignmentId: id, tabId: approved.identity.tabId,
          receiptSha256: approved.receipt.sha256, reviewSha256: approved.reviewSha256, run });
      } catch (error) {
        if (error.code === 'EEXIST') throw new Refusal('settlement_intent_already_exists');
        throw error;
      }
      stage = 'recheck';
      requireThat(isDeepStrictEqual(await acceptance(opts, observe), approved), 'acceptance_changed');
      const recorded = await readRecord(acceptedFile, 'previous_acceptance_unreadable');
      requireThat(isDeepStrictEqual(recorded.value, approved), 'previous_acceptance_differs');
      opts.signal?.throwIfAborted();
      stage = 'settlement_requested';
      await settle(approved.identity.tabId, opts.signal);
      stage = 'confirmation';
      requireThat(isDeepStrictEqual(await acceptance(opts, observe, 'after'), approved), 'acceptance_changed');
      state = 'settled';
    }
    opts.signal?.throwIfAborted();
    const final = { state, assignmentId: id, tabId: approved.identity.tabId, run };
    await createRecord(resultFile, final);
    return final;
  } catch (error) {
    const reason = opts.signal?.aborted || error.name === 'AbortError' ? 'interrupted' :
      error instanceof Refusal ? error.message : error.code === 'EEXIST' ? 'stand_down_already_in_progress' : 'operation_failed';
    const failure = { state: 'incomplete', assignmentId: id, stage, reason, run };
    if (resultFile) {
      try { await createRecord(resultFile, failure); }
      catch { return { ...failure, persistence: 'unconfirmed' }; }
    }
    return failure;
  } finally {
    if (lock) await rmdir(lock).catch(() => {}); // A surviving lock fails closed for the next invocation.
  }
}

const usage = 'Usage: node stand-down.mjs --registry FILE --assignment ID --review FILE --observer /absolute/program ' +
  '[--receipt-store DIR] [--supplement FILE] [--dry-run | --persist DIR [--settle]]';

async function main(argv) {
  const options = Object.fromEntries(['registry', 'assignment', 'review', 'observer', 'receipt-store',
    'supplement', 'persist', 'desktop'].map(option => [option, { type: 'string' }]));
  for (const option of ['dry-run', 'settle', 'help']) options[option] = { type: 'boolean' };
  let opts;
  try {
    const { values } = parseArgs({ args: argv, options, strict: true });
    if (values.help) { console.log(usage); return 0; }
    requireThat(text(values.registry) && text(values.review) && matches(assignmentPattern, values.assignment) &&
      absolute(values.observer) && (values.desktop === undefined || absolute(values.desktop)) &&
      ['receipt-store', 'supplement'].every(option => values[option] === undefined || text(values[option])) &&
      (!values['dry-run'] || values.persist === undefined) && (!values.settle || text(values.persist)) &&
      (values.persist === undefined || text(values.persist)), 'invalid_request');
    opts = { ...values, receiptStore: values['receipt-store'] };
  } catch {
    console.error(usage);
    return 2;
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  process.once('SIGINT', abort);
  process.once('SIGTERM', abort);
  const transport = { timeout: 5000, maxBuffer: 256 * 1024, windowsHide: true };
  try {
    const result = await standDown({ ...opts, signal: controller.signal }, {
      async observe(identity, signal) {
        try {
          const { stdout } = await exec(opts.observer, [identity.assignmentId, identity.terminalId, identity.tabId],
            { ...transport, signal });
          return JSON.parse(stdout);
        } catch { throw new Refusal('herdr_observation_unavailable'); }
      },
      async settle(tab, signal) {
        try {
          await exec(opts.desktop ?? 'herdr-app', ['do', 'tab.settle', '--id', tab], { ...transport, signal });
        } catch { throw new Refusal('desktop_settlement_unconfirmed'); }
      },
    });
    console.log(JSON.stringify(result));
    return result.state === 'incomplete' ? 1 : 0;
  } finally {
    process.removeListener('SIGINT', abort);
    process.removeListener('SIGTERM', abort);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main(process.argv.slice(2));
}
