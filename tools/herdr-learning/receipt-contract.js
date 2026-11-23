import { createHash, randomUUID } from 'node:crypto';
import { link, mkdir, readFile, realpath, stat, unlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const idPattern = /^[a-f0-9]{32}$/;
const outcomes = ['completed', 'blocked', 'failed'];
const text = { type: 'string', minLength: 1, maxLength: 4000 };
const learningFields = ['worked', 'wentWrong', 'carryForward'];
const parameters = {
  type: 'object',
  additionalProperties: false,
  required: ['assignmentId', 'task', 'outcome', 'summary', 'checks', 'artifacts', 'learning'],
  properties: {
    assignmentId: { type: 'string', pattern: idPattern.source },
    task: text,
    outcome: { type: 'string', enum: outcomes },
    summary: text,
    learning: {
      type: 'object',
      additionalProperties: false,
      required: learningFields,
      properties: { worked: text, wentWrong: text, carryForward: text },
    },
    checks: {
      type: 'array', maxItems: 20,
      items: {
        type: 'object', additionalProperties: false, required: ['command', 'exitCode'],
        properties: { command: text, exitCode: { type: 'integer' } },
      },
    },
    artifacts: { type: 'array', maxItems: 30, items: text },
  },
};

async function receiptPath(cwd, assignmentId, store) {
  if (!idPattern.test(assignmentId)) throw new Error('Expected an exact 32-character lowercase hexadecimal assignment ID.');
  const project = await realpath(cwd);
  const key = createHash('sha256').update(project).digest('hex');
  return { project, path: join(store, key, assignmentId + '.json') };
}

export const defaultTeamReceiptStore = join(homedir(), '.local/share/pi-coding-agent/team-receipts');

export async function readTeamReceipt(cwd, assignmentId, store = defaultTeamReceiptStore) {
  const { path } = await receiptPath(cwd, assignmentId, store);
  return { path, receipt: JSON.parse(await readFile(path, 'utf8')) };
}

export function registerTeamReceipt(pi, store = defaultTeamReceiptStore) {
  pi.on('before_agent_start', (event, ctx) => {
    if (ctx.hasUI) {
      event.systemPromptOptions.sections.team_learning_handoff =
        'Before normal assignment handoff, call team_completion_receipt with evidence and learning: ' +
        'what worked, what went wrong, and what to carry forward. Explain when nothing was observed. ' +
        'Report interrupted or blocked work accurately. This applies to every named role and subagent.';
    }
  });
  pi.registerCommand('receipt-contract', {
    description: 'Show the active team receipt contract',
    handler: async (_args, ctx) => {
      ctx.ui.notify('Team receipt v2: worked, wentWrong and carryForward required.', 'info');
    },
  });
  pi.registerTool({
    name: 'team_completion_receipt',
    label: 'Team completion receipt',
    description: 'Record the final outcome for an assigned Herdr task. Report exact commands actually run and existing file paths, and what worked, went wrong, and to carry forward. Explain if nothing was observed. This is a worker report for controller verification.',
    parameters,
    async execute(_id, input, signal, _onUpdate, ctx) {
      if (!ctx.hasUI || !ctx.isProjectTrusted()) throw new Error('A trusted interactive Pi session is required.');
      const role = ctx.sessionManager.getSessionName();
      if (!role || !ctx.model) throw new Error('A named Pi session and resolved model are required.');
      const learning = {};
      for (const field of learningFields) {
        const value = input.learning?.[field];
        if (typeof value !== 'string' || !value.trim() || value.length > text.maxLength) {
          throw new Error(`Learning handoff requires ${field}. Explain if nothing was observed.`);
        }
        learning[field] = value.trim();
      }
      const { project, path } = await receiptPath(ctx.cwd, input.assignmentId, store);
      const artifacts = await Promise.all(input.artifacts.map(async artifact => {
        const file = await realpath(resolve(project, artifact));
        if (!(await stat(file)).isFile()) throw new Error('Expected an artifact file: ' + artifact);
        return file;
      }));
      const directory = resolve(path, '..');
      const temp = join(directory, '.' + randomUUID() + '.tmp');
      const receipt = {
        version: 2,
        assignmentId: input.assignmentId,
        project,
        role,
        sessionId: ctx.sessionManager.getSessionId(),
        model: { provider: ctx.model.provider, id: ctx.model.id, thinking: ctx.thinkingLevel },
        outcome: input.outcome,
        task: input.task,
        summary: input.summary,
        learning,
        checks: input.checks,
        artifacts,
        recordedAt: new Date().toISOString(),
      };
      signal?.throwIfAborted();
      await mkdir(directory, { recursive: true, mode: 0o700 });
      try {
        await writeFile(temp, JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
        signal?.throwIfAborted();
        await link(temp, path);
      } catch (error) {
        if (error.code === 'EEXIST') throw new Error('A receipt already exists for this assignment. Read it at ' + path);
        throw error;
      } finally {
        await unlink(temp).catch(error => {
          if (error.code !== 'ENOENT') throw error;
        });
      }
      ctx.ui.notify(`${role}: ${input.outcome}. Completion receipt: ${path}`, 'info');
      return { content: [{ type: 'text', text: 'Completion receipt: ' + path }], details: { path } };
    },
  });
}

export default function (pi) {
  registerTeamReceipt(pi);
}
