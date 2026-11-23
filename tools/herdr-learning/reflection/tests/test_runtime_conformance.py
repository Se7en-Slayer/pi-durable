"""Runtime adapter conformance: a scripted transport, no Herdr/Codex process."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(os.environ.get('UO_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(SOURCE))
import herdr_graph_runtime as runtime


class Interrupted(BaseException):
    """Pretend the process died without an opportunity to reconcile."""


class RuntimeConformance(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='uo04-runtime-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.goals = self.root / 'goals'
        self.goals.mkdir()
        self.goal = self.goals / 'project.json'
        source = self.root / 'source.txt'
        source.write_bytes(b'fixture source\n')
        pinned_source = dict(kind='artifact', path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        participants = [dict(id=who, parent=parent, role=role, herdr=dict(terminal_id=terminal, created_ms=generation))
                        for who, parent, role, terminal, generation in (
                            ('cos', None, 'coordinator', 't1', 11), ('owner', 'cos', 'analysis', 't2', 22),
                            ('peer', 'owner', 'documentation', 't3', 33), ('reviewer', 'cos', 'review', 't4', 44))]
        self.record = dict(schema_version=2, project_id='project', coordinator='cos', held=False, participants=participants,
                           deliverables=[dict(id=name, objective='synthetic', owner=owner, scope=f'fixture:{name}', level=1, held=False,
                                              status='planned', dependencies=[], source=pinned_source,
                                              required_artifacts=['result'], attempts=[])
                                         for name, owner in (('primary', 'owner'), ('unrelated', 'peer'))])
        self.write_record(self.record)
        self.message = self.root / 'message.txt'
        self.message.write_text('Synthetic assignment. No real commands.\n', encoding='utf-8')
        self.terminals = {h['terminal_id']: dict(address=h['terminal_id'], created_ms=h['created_ms'],
                                                state='live', status={'app': 'pi', 'state': 'idle'})
                          for h in (p['herdr'] for p in participants)}
        self.screens = {}
        self.send_calls = []
        self.calls = []
        self.after_admission = None
        self.on_send = None

    def write_record(self, record):
        self.goal.write_text(json.dumps(record), encoding='utf-8')

    def read_record(self):
        return json.loads(self.goal.read_text(encoding='utf-8'))

    def task(self, key='primary'):
        return next(task for task in self.read_record()['deliverables'] if task['id'] == key)

    def fake_herdr(self, *args):
        self.calls.append(args)
        if self.after_admission and self.read_record().get('runtime_admissions'):
            hook, self.after_admission = self.after_admission, None
            hook()
        if args[:2] == ('ls', '--json') and len(args) == 2:
            return json.dumps({'terminals': list(self.terminals.values())})
        if args[:2] == ('get', '--json') and len(args) == 3:
            terminal = args[2]
            if terminal not in self.terminals:
                raise subprocess.CalledProcessError(1, args)
            return json.dumps(self.terminals[terminal])
        if args[:3] == ('read', '--lines', '0') and len(args) == 4:
            return self.screens.get(args[3], '')
        if args[:4] == ('send', '--json', '--if', 'idle') and len(args) == 6:
            address, payload = args[4:]
            assignment_line, scope_line, message = payload.split('\n', 2)
            assignment = assignment_line.removeprefix('Assignment-ID: ')
            admission = self.read_record()['runtime_admissions'][assignment]
            self.assertEqual(assignment_line, f'Assignment-ID: {admission["assignment_id"]}')
            self.assertEqual(scope_line, f'Registered scope: {admission["scope"]}')
            self.assertEqual(message, self.message.read_text(encoding='utf-8'))
            self.assertEqual(address, admission['herdr']['terminal_id'])
            self.assertEqual(self.terminals[address]['status']['state'], 'idle')
            self.send_calls.append(args)
            if self.on_send:
                self.on_send()
            return json.dumps({'status': 'idle', 'turn': 17, 'started': True, 'wait': None})
        self.fail(f'Unexpected transport call {args!r}')

    def command(self, action, task='primary', challenge=None):
        args = [action, self.goal.name, task, '--message-file', str(self.message)]
        if challenge is not None:
            args.extend(['--challenge', challenge])
        with patch.dict(os.environ, {'HC_TERMINAL_ID': 't1'}), redirect_stdout(io.StringIO()) as output:
            status = runtime.command(self.root, self.fake_herdr, args)
        return status, json.loads(output.getvalue())

    def preflight(self, task='primary'):
        status, result = self.command('preflight', task)
        self.assertEqual(status, 0, result)
        self.assertEqual(result['scope'], f'fixture:{task}')
        return result

    def assert_admitted_without_replay(self, challenge, expected_calls):
        [attempt] = self.task()['attempts']
        self.assertEqual((attempt['number'], attempt['state']), (1, 'active'))
        self.assertNotIn('settlement', attempt)
        before = self.goal.read_bytes()
        status, result = self.command('assign', challenge=challenge)
        self.assertNotEqual(status, 0, result)
        self.assertEqual(self.goal.read_bytes(), before)
        self.assertEqual(len(self.send_calls), expected_calls)

    def test_pause_between_preflight_and_assign_is_read_only(self):
        challenge = self.preflight()['challenge']
        record = self.read_record()
        record['held'] = True
        self.write_record(record)
        before = self.goal.read_bytes()
        status, result = self.command('assign', challenge=challenge)
        self.assertNotEqual(status, 0, result)
        self.assertEqual(self.goal.read_bytes(), before)
        self.assertEqual(self.send_calls, [])

    def test_late_owner_hold_after_admission_stops_transport_and_replay(self):
        challenge = self.preflight()['challenge']
        self.after_admission = lambda: self.screens.update(t2='\n[[OWNER_DECISION_REQUIRED]]\n')
        status, result = self.command('assign', challenge=challenge)
        self.assertNotEqual(status, 0, result)
        self.assertEqual(self.send_calls, [])
        self.screens.clear()
        self.assert_admitted_without_replay(challenge, 0)

    def test_interruption_after_admission_before_transport_does_not_replay(self):
        challenge = self.preflight()['challenge']

        def interrupt():
            raise Interrupted()

        self.after_admission = interrupt
        with self.assertRaises(Interrupted):
            self.command('assign', challenge=challenge)
        self.assert_admitted_without_replay(challenge, 0)

    def test_uncertain_transport_return_does_not_replay_and_unrelated_dispatches(self):
        challenge = self.preflight()['challenge']

        def timeout():
            raise subprocess.TimeoutExpired('in-memory fake transport', 1)

        self.on_send = timeout
        status, result = self.command('assign', challenge=challenge)
        self.assertNotEqual(status, 0, result)
        self.assertEqual(result['state'], 'reconciliation_required')
        self.assert_admitted_without_replay(challenge, 1)
        self.on_send = None
        peer = self.preflight('unrelated')
        self.assertEqual(self.command('assign', 'unrelated', peer['challenge'])[0], 0)
        self.assertEqual([call[4] for call in self.send_calls], ['t2', 't3'])

    def test_interruption_after_transport_before_recording_return_does_not_replay(self):
        challenge = self.preflight()['challenge']
        real_artifact = runtime.artifact

        def interrupt_ack(directory, assignment, name, body, **kwargs):
            if name == 'transport-returned':
                self.assertEqual(body, {'assignment_id': assignment, 'turn': 17})
                raise Interrupted()
            return real_artifact(directory, assignment, name, body, **kwargs)

        with patch.object(runtime, 'artifact', side_effect=interrupt_ack), self.assertRaises(Interrupted):
            self.command('assign', challenge=challenge)
        self.assert_admitted_without_replay(challenge, 1)
        self.assertEqual(list((self.root / 'runtime-evidence').glob('*-transport-returned.json')), [])

    def test_missing_terminal_does_not_settle_or_recover_the_active_writer(self):
        challenge = self.preflight()['challenge']
        self.assertEqual(self.command('assign', challenge=challenge)[0], 0)
        self.terminals.pop('t2')
        before = self.goal.read_bytes()
        stable = {}
        self.assertFalse(runtime.observe(self.root, self.goal, self.fake_herdr, stable, 0))
        self.assertFalse(runtime.observe(self.root, self.goal, self.fake_herdr, stable, 0))
        self.assertEqual(self.goal.read_bytes(), before)
        self.assert_admitted_without_replay(challenge, 1)

    def test_stable_idle_checkpoint_only_records_one_reconciliation_observation(self):
        admission = self.preflight()
        self.assertEqual(self.command('assign', challenge=admission['challenge'])[0], 0)
        stable = {}
        with patch.object(runtime.time, 'monotonic', side_effect=[100, 104, 105, 106]):
            self.assertFalse(runtime.observe(self.root, self.goal, self.fake_herdr, stable, 5))
            self.assertFalse(runtime.observe(self.root, self.goal, self.fake_herdr, stable, 5))
            self.assertTrue(runtime.observe(self.root, self.goal, self.fake_herdr, stable, 5))
            self.assertFalse(runtime.observe(self.root, self.goal, self.fake_herdr, stable, 5))
        [observation] = self.read_record()['runtime_recovery'].values()
        [attempt] = self.task()['attempts']
        self.assertEqual(observation['assignment_id'], admission['assignment_id'])
        self.assertEqual(observation['checkpoint'], attempt['checkpoints'][-1])
        self.assertEqual(observation['herdr'], attempt['herdr'])
        self.assertEqual(attempt['state'], 'active')
        self.assertEqual(len(self.send_calls), 1)

    def test_late_terminal_generation_change_blocks_transport(self):
        challenge = self.preflight()['challenge']
        self.after_admission = lambda: self.terminals['t2'].update(created_ms=222)
        status, result = self.command('assign', challenge=challenge)
        self.assertNotEqual(status, 0, result)
        self.assert_admitted_without_replay(challenge, 0)
