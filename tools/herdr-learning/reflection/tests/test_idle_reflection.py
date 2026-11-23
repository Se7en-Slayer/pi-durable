"""Behavioral tests of the candidate watchdog and CoS reflection route. No live Herdr."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import herdr_graph_runtime as runtime
import herdr_idle_reflection as reflection

ROOT = Path(__file__).resolve().parents[1]


class IdleReflection(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.directory = self.home / '.local/state/herdr-idle-healer'
        (self.directory / "goals").mkdir(parents=True)
        self.goal = self.directory / "goals/example.json"
        self.stable = {}
        self.calls = []
        self.now = 0
        self.core = {}
        self.screens = {}
        self.fail_send = False
        self.project = {"schema_version": 2, "project_id": "example", "coordinator": "root",
                        "participants": [], "deliverables": [], "reflection_bindings": []}
        self.add_actor("root", None, 1, "Chief of Staff")
        self.add_actor("maker", "root", 2, "Engineering")
        self.add_actor("nested", "maker", 3, "Documentation")
        self.add_binding("maker", "a" * 32)
        self.save()

    def artifact(self, name, data):
        path = self.directory / (name + ".json")
        raw = runtime.encoded(data)
        path.write_bytes(raw)
        return {"name": name, "path": str(path), "sha256": runtime.digest(raw)}

    def add_actor(self, identifier, parent, number, role):
        address = f"t{number}"
        self.project["participants"].append({"id": identifier, "parent": parent, "role": role,
                                            "herdr": {"terminal_id": address, "created_ms": number * 100}})
        self.core[address] = {"address": address, "created_ms": number * 100, "turn": 4,
                              "state": "live", "reported": True, "status_ms": 777,
                              "status": {"state": "done"}}

    def add_binding(self, participant, assignment, *, handle=None, project_id=None):
        if handle is None:
            handle = next(row["herdr"] for row in self.project["participants"] if row["id"] == participant)
        identity = {"participant_id": participant, "project_id": project_id or self.project["project_id"],
                    "assignment_id": assignment, "herdr": handle, "turn": 4}
        self.project["reflection_bindings"].append({**{k: v for k, v in identity.items() if k != "project_id"},
            "held": False, "paused": False, "stood_down": False,
            "evidence": self.artifact(participant + "-settled", {**identity, "state": "settled"})})
        return identity

    def save(self):
        self.goal.write_bytes(runtime.encoded(self.project))

    def state(self):
        return json.loads(self.goal.read_bytes())

    def herdr(self, *argv):
        self.calls.append(argv)
        if argv[:2] == ("get", "--json"):
            value = self.core[argv[-1]]
            if value is None:
                raise subprocess.CalledProcessError(1, argv)
            return json.dumps(value)
        if argv[:3] == ("read", "--lines", "0"):
            return self.screens.get(argv[-1], "")
        if argv == ("ls", "--json"):
            return json.dumps({"terminals": list(self.core.values())})
        if argv[:4] == ("send", "--json", "--if", "idle"):
            target, message = argv[4:]
            self.assertEqual(self.core[target]['status']['state'], 'idle')
            matches = [delivery for row in self.queue().values() for key in ('notification', 'handoff')
                       if (delivery := row.get(key)) and delivery['state'] == 'intent' and
                       delivery['recipient']['terminal_id'] == target and delivery['reflection_id'] in message]
            self.assertEqual(len(matches), 1)
            if target != 't1':
                self.assertIn(matches[0]['id'], message)
            if self.fail_send:
                raise subprocess.CalledProcessError(1, argv)
            self.core[target]['turn'] += 1
            self.core[target]['status']['state'] = 'working'
            return json.dumps({'turn': self.core[target]['turn'], 'status': 'idle'})
        self.fail(f"unexpected side effect {argv[0]}")

    def tick(self, at, legacy_ready=lambda _project: True):
        self.now = at
        return reflection.observe(self.directory, self.goal, self.herdr, self.stable, legacy_ready,
                                  clock=lambda: self.now)

    def accrue(self, start=0, elapsed=590):
        for at in range(start, start + elapsed + 1, 30):
            self.assertEqual(self.tick(at), 0)
        if elapsed % 30:
            self.assertEqual(self.tick(start + elapsed), 0)

    def queue(self):
        return self.state().get("runtime_reflections", {})

    def invoke(self, actor, *arguments):
        out = io.StringIO()
        with patch.dict(os.environ, {"HC_TERMINAL_ID": actor}), contextlib.redirect_stdout(out):
            result = reflection.command(self.directory, self.herdr,
                [arguments[0], self.goal.name, *arguments[1:]], lambda _goal: True)
        return result, json.loads(out.getvalue())

    def test_threshold_role_neutral_scope_and_restart_dedup(self):
        self.add_binding("root", "b" * 32)
        self.add_binding("nested", "c" * 64)
        self.save()
        self.accrue(elapsed=599)
        self.assertEqual(self.queue(), {})
        self.assertEqual(self.tick(600), 3)
        queued = self.queue()
        self.assertEqual({r["participant_id"] for r in queued.values()}, {"root", "maker", "nested"})
        for key, row in queued.items():
            self.assertEqual(key, reflection.reflection_id(row))
            self.assertEqual(row["state"], "coordinator_review_required")
            self.assertEqual(row["scope"], reflection.scope(row))
            self.assertEqual(row["stable_seconds"], 600)
        self.assertEqual(self.tick(630), 0)
        self.stable = {}  # New watchdog instance, same persisted goal.
        self.accrue(start=1000, elapsed=600)
        self.assertEqual(self.queue(), queued)

    def test_gap_after_590_restarts_full_600_seconds(self):
        self.accrue(elapsed=590)
        self.assertEqual(self.tick(650), 0)
        self.accrue(start=650, elapsed=599)
        self.assertEqual(self.tick(1250), 1)

    def test_probe_failure_lock_and_hold_reset_unproven_time(self):
        self.accrue(elapsed=590)
        raw = self.core['t2']
        self.core['t2'] = None
        self.assertEqual(self.tick(600), 0)
        self.assertEqual(self.stable, {})
        self.core['t2'] = raw
        self.accrue(start=630)
        with runtime.lock(self.directory), self.assertRaises(BlockingIOError):
            self.tick(1230)
        self.assertEqual(self.stable, {})
        self.accrue(start=1260)
        self.screens['t2'] = runtime.OWNER_HOLD
        self.assertEqual(self.tick(1860), 0)
        self.assertEqual(self.stable, {})
        del self.screens['t2']
        self.accrue(start=1890, elapsed=599)
        self.assertEqual(self.tick(2490), 1)
        queued = self.queue()
        self.project = self.state()
        self.project['held'] = True
        self.save()
        self.assertEqual(self.tick(2520), 0)
        self.assertEqual(self.queue(), queued)

    def test_ineligible_states_and_stale_or_foreign_evidence(self):
        binding = self.project['reflection_bindings'][0]
        raw = self.core['t2']
        for flag in ('held', 'paused', 'stood_down'):
            binding[flag] = True
            self.save()
            self.accrue(elapsed=600)
            self.assertFalse(self.stable)
            binding[flag] = False
        self.save()
        for field, value in (('status', {'state': 'working'}), ('turn', 9), ('created_ms', 201), ('reported', False)):
            with patch.dict(raw, {field: value}):
                self.accrue(elapsed=600)
                self.assertFalse(self.stable)
        path = Path(binding['evidence']['path'])
        proof = json.loads(path.read_bytes())
        path.unlink()
        self.accrue(elapsed=600)
        self.assertFalse(self.stable)
        binding['evidence'] = self.artifact('foreign', {**proof, 'assignment_id': 'z' * 32})
        self.save()
        self.accrue(elapsed=600)
        self.assertFalse(self.stable)

    def test_recovery_and_cross_project_conflict_defer_without_replay(self):
        record = {'assignment_id': 'a' * 32, 'state': 'coordinator_reconciliation_required'}
        self.project['runtime_recovery'] = {'preserved': record}
        self.save()
        self.accrue(elapsed=600)
        self.assertEqual(self.state()['runtime_recovery'], {'preserved': record})
        self.assertFalse(self.queue())
        self.project.pop('runtime_recovery')
        self.save()
        other = self.directory / 'goals/peer.json'
        bystander = {'schema_version': 2, 'project_id': 'peer', 'deliverables': [
            {'attempts': [{'state': 'active', 'herdr': {'terminal_id': 't2', 'created_ms': 200}}]}]}
        other.write_bytes(runtime.encoded(bystander))
        self.accrue(elapsed=600)
        self.assertFalse(self.queue())
        bystander['deliverables'] = []
        bystander['reflection_bindings'] = self.project['reflection_bindings']
        other.write_bytes(runtime.encoded(bystander))
        self.accrue(elapsed=600)
        self.assertEqual(self.calls, [])
        other.unlink()
        self.accrue(start=660, elapsed=599)
        self.assertEqual(self.tick(1260), 1)
        self.assertTrue(all(call[0] in {'get', 'read'} for call in self.calls))

    def test_legacy_does_not_infer_and_explicit_registration_yields_to_recovery(self):
        self.project = {'state': 'complete', 'supervisor': {'address': 't1', 'created_ms': 100},
                        'workers': [{'address': 't2', 'created_ms': 200}], 'deliverables': []}
        self.save()
        self.accrue(elapsed=600)
        self.assertEqual(self.calls, [])
        self.project.update(reflection_project_id='legacy', reflection_bindings=[])
        self.add_binding('t2', 'a'*32, project_id='legacy', handle={'terminal_id': 't2', 'created_ms': 200})
        self.save()
        for at in range(0, 601, 30):
            self.assertEqual(self.tick(at, lambda _goal: False), 0)
        self.assertFalse(self.queue())
        self.accrue(start=630, elapsed=599)
        self.assertEqual(self.tick(1230), 1)

    def test_route_and_record_require_bound_actors_durable_admission_and_evidence(self):
        self.accrue(elapsed=599)
        self.assertEqual(self.invoke('t1', 'reflection-route', '1' * 64)[0], 3)
        self.assertEqual(self.tick(600), 1)
        [(key, queued)] = self.queue().items()
        subject = {name: queued[name] for name in ('project_id', 'participant_id', 'assignment_id', 'herdr', 'turn')}
        body = {**subject, 'reflection_id': key, 'handoff_id': '0'*64,
                **{field: 'Observed fixture result.' for field in reflection.FIELDS},
                'evidence': [self.artifact('underlying-log', {'observed': True})]}
        path = self.directory / 'result.json'
        path.write_bytes(runtime.encoded(body))
        options = ('reflection-record', key, '--proof-file', str(path))
        self.assertEqual(self.invoke('t2', *options)[0], 3)
        self.assertEqual(self.invoke('t2', 'reflection-route', key)[0], 3)
        self.assertEqual(self.invoke('t1', 'reflections')[1], self.queue())
        self.core['t2']['status']['state'] = 'idle'
        self.assertEqual(self.invoke('t1', 'reflection-route', key)[1]['state'], 'routed')
        self.assertEqual(self.invoke('t2', *options)[0], 3)  # Wrong handoff ID.
        body['handoff_id'] = self.queue()[key]['handoff']['id']
        path.write_bytes(runtime.encoded(body))
        self.assertEqual(self.invoke('t1', *options)[0], 3)
        self.core['t2']['turn'] = 8  # Actor reports on the earlier original turn.
        path.write_bytes(runtime.encoded({**body, 'assignment_id': 'b' * 32}))
        self.assertEqual(self.invoke('t2', *options)[0], 3)
        path.write_bytes(runtime.encoded({**body, 'evidence': []}))
        self.assertEqual(self.invoke('t2', *options)[0], 3)
        path.write_bytes(runtime.encoded(body))
        self.assertEqual(self.invoke('t2', *options)[1]['state'], 'recorded')
        self.assertEqual(self.queue()[key]['result'], body)
        self.assertEqual(self.invoke('t2', *options)[0], 3)
        self.stable = {}
        self.core['t2']['turn'] = 4
        self.accrue(start=660, elapsed=600)
        self.assertEqual(self.queue()[key]['state'], 'recorded')

    def test_done_coordinator_and_done_worker_are_never_sent(self):
        self.accrue(elapsed=599)
        self.assertEqual(self.tick(600), 1)
        [(key, row)] = self.queue().items()
        self.assertNotIn('notification', row)
        self.assertFalse([call for call in self.calls if call[0] == 'send'])
        self.core['t1']['status']['state'] = 'idle'
        self.assertEqual(self.tick(630), 0)
        self.assertEqual(self.queue()[key]['notification']['state'], 'delivered')
        self.assertEqual(self.invoke('t1', 'reflection-route', key)[0], 3)
        self.assertNotIn('handoff', self.queue()[key])
        self.assertEqual([call[4] for call in self.calls if call[0] == 'send'], ['t1'])
        self.core['t2']['status']['state'] = 'idle'
        self.assertEqual(self.invoke('t1', 'reflection-route', key)[0], 0)
        self.assertEqual([call[4] for call in self.calls if call[0] == 'send'], ['t1', 't2'])

    def test_uncertain_coordinator_and_worker_intents_are_not_replayed(self):
        self.core['t1']['status']['state'] = 'idle'
        self.fail_send = True
        self.accrue(elapsed=599)
        self.assertEqual(self.tick(600), 1)
        [(key, row)] = self.queue().items()
        self.assertEqual(row['notification']['state'], 'uncertain')
        self.stable = {}
        self.assertEqual(self.tick(630), 0)
        self.core['t2']['status']['state'] = 'idle'
        self.assertEqual(self.invoke('t1', 'reflection-route', key)[0], 4)
        self.assertEqual(self.queue()[key]['handoff']['state'], 'uncertain')
        self.assertEqual(self.invoke('t1', 'reflection-route', key)[0], 3)
        self.assertEqual([call[4] for call in self.calls if call[0] == 'send'], ['t1', 't2'])

    def test_idle_coordinator_hold_and_competing_work_wait_without_notification(self):
        self.core['t1']['status']['state'] = 'idle'
        self.screens['t1'] = runtime.OWNER_HOLD
        self.accrue(elapsed=599)
        self.assertEqual(self.tick(600), 1)
        [(key, row)] = self.queue().items()
        self.assertNotIn('notification', row)
        self.assertFalse([call for call in self.calls if call[0] == 'send'])
        del self.screens['t1']
        peer = self.directory/'goals/peer.json'
        peer.write_bytes(runtime.encoded({'schema_version': 2, 'deliverables': [{'attempts': [
            {'state': 'active', 'herdr': {'terminal_id': 't1', 'created_ms': 100}}]}]}))
        self.assertEqual(self.tick(630), 0)
        self.assertNotIn('notification', self.queue()[key])
        peer.unlink()
        self.assertEqual(self.tick(660), 0)
        self.assertEqual(self.queue()[key]['notification']['state'], 'delivered')
        self.assertEqual([call[4] for call in self.calls if call[0] == 'send'], ['t1'])

    def test_graph_link_requires_exact_runtime_admission_and_settlement(self):
        aid = reflection.attempt_id('example', 'task', 1)
        self.project['reflection_bindings'] = []
        identity = self.add_binding('maker', aid)
        self.project['reflection_bindings'][0]['task_id'] = 'task'
        start = {key: identity[key] for key in ('assignment_id','herdr','project_id')}
        start.update(task_id='task', owner='maker', scope='write scope')
        proof = self.artifact('admission', start)
        checkpoint = {'stage': 'dispatched', 'evidence': proof}
        settlement = self.artifact('settlement', {key: start[key] for key in ('assignment_id','herdr','scope')} | {
            'checkpoint': checkpoint, 'state': 'settled', 'observation': self.artifact('settled-log', {'fixture': True})})
        self.project['deliverables'] = [{'id': 'task', 'owner': 'maker', 'level': 1, 'status': 'implemented',
            'attempts': [{'id': aid, 'number': 1, 'owner': 'maker', 'herdr': identity['herdr'], 'scope': 'write scope',
                          'state': 'settled', 'checkpoints': [checkpoint], 'settlement': settlement}]}]
        self.save()
        self.accrue(elapsed=600)
        self.assertFalse(self.queue())
        self.project['runtime_admissions'] = {aid: {**start, 'evidence': proof}}
        self.save()
        self.accrue(start=630, elapsed=599)
        self.assertEqual(self.tick(1230), 1)

    def assert_post_intent_change_blocks(self, field, change):
        self.accrue(elapsed=599)
        self.core['t1']['status']['state'] = 'idle' if field == 'notification' else 'done'
        if field == 'handoff':
            self.assertEqual(self.tick(600), 1)
            self.core['t2']['status']['state'] = 'idle'
        persist = reflection.persist
        changed = []

        def mutate_after_persist(goal, raw, project):
            persist(goal, raw, project)
            if changed or not any(row.get(field, {}).get('state') == 'intent' for row in self.queue().values()):
                return
            before = goal.read_bytes()
            if change in ('worker_hold', 'cos_hold'):
                self.screens['t2' if change == 'worker_hold' else 't1'] = runtime.OWNER_HOLD
            elif change == 'proof':
                proof = Path(self.project['reflection_bindings'][0]['evidence']['path'])
                proof.write_bytes(proof.read_bytes() + b' ')
            elif change == 'turn':
                self.core['t2']['turn'] += 1
            elif change == 'cos_generation':
                self.core['t1']['created_ms'] += 1
            self.assertEqual(goal.read_bytes(), before)
            changed.append(True)

        with patch.object(reflection, 'persist', mutate_after_persist):
            if field == 'notification':
                self.assertEqual(self.tick(600), 1)
            else:
                key, = self.queue()
                self.assertEqual(self.invoke('t1', 'reflection-route', key)[0], 4)
        self.assertEqual(changed, [True])
        [(key, row)] = self.queue().items()
        self.assertEqual(row[field]['state'], 'blocked_before_send')
        self.assertEqual(row['state'], 'coordinator_review_required')
        # Restore every external precondition: a persisted blocked intent still cannot replay.
        self.screens.clear()
        self.core['t2']['turn'] = 4
        self.core['t1']['created_ms'] = 100
        if change == 'proof':
            proof = Path(self.project['reflection_bindings'][0]['evidence']['path'])
            proof.write_bytes(proof.read_bytes()[:-1])
        self.stable = {}
        self.assertEqual(self.tick(630), 0)
        if field == 'handoff':
            self.assertEqual(self.invoke('t1', 'reflection-route', key)[0], 3)
        self.assertEqual(self.queue()[key], row)
        self.assertFalse([call for call in self.calls if call[0] == 'send'])

    def test_notify_rechecks_subject_hold_after_intent(self):
        self.assert_post_intent_change_blocks('notification', 'worker_hold')

    def test_notify_rechecks_subject_proof_after_intent(self):
        self.assert_post_intent_change_blocks('notification', 'proof')

    def test_notify_rechecks_subject_turn_after_intent(self):
        self.assert_post_intent_change_blocks('notification', 'turn')

    def test_handoff_rechecks_worker_hold_after_intent(self):
        self.assert_post_intent_change_blocks('handoff', 'worker_hold')

    def test_handoff_rechecks_cos_hold_after_intent(self):
        self.assert_post_intent_change_blocks('handoff', 'cos_hold')

    def test_handoff_rechecks_cos_generation_after_intent(self):
        self.assert_post_intent_change_blocks('handoff', 'cos_generation')

    def test_handoff_rechecks_subject_proof_after_intent(self):
        self.assert_post_intent_change_blocks('handoff', 'proof')

    def test_coordinator_self_routes_without_second_send(self):
        self.project['reflection_bindings'] = []
        self.add_binding('root', 'f'*32)
        self.save()
        self.core['t1']['status']['state'] = 'idle'
        self.accrue(elapsed=599)
        self.assertEqual(self.tick(600), 1)
        [(key, _)] = self.queue().items()
        self.assertEqual(self.invoke('t1', 'reflection-route', key)[0], 0)
        self.assertEqual(self.queue()[key]['handoff']['state'], 'self_routed')
        self.assertEqual([call[4] for call in self.calls if call[0] == 'send'], ['t1'])

    def test_existing_watchdog_tick_not_a_new_scheduler(self):
        self.core['t1']['status']['state'] = 'idle'
        shell = (ROOT / 'herdr-idle-healer.sh').read_text()
        body = shell.split("<<'PY'\n", 1)[1].rsplit('\nPY\n', 1)[0]
        namespace = {'__name__': 'watch_fixture'}
        with patch.dict(os.environ, {'HERDR_HEALER_LIB_DIR': str(ROOT)}):
            exec(compile(body, str(ROOT / 'herdr-idle-healer.sh'), 'exec'), namespace)
        namespace['herdr'] = self.herdr
        namespace['launch'] = lambda *_: self.fail('recovery was replayed')
        namespace['log'] = lambda *_: None
        ticks = iter(range(0, 601, 30))
        class Finished(BaseException):
            pass
        def sleep(_):
            try:
                self.now = next(ticks)
            except StopIteration:
                raise Finished
        original_observe = reflection.observe
        def fake_clock(*args):
            return original_observe(*args, clock=lambda: self.now)
        with patch.object(namespace['time'], 'sleep', sleep), patch.object(reflection, 'observe', fake_clock), self.assertRaises(Finished):
            namespace['watch'](self.directory, 3, 3, 'read-only')
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual([call[4] for call in self.calls if call[0] == 'send'], ['t1'])
        [(key, row)] = self.queue().items()
        self.assertEqual(row['notification']['state'], 'delivered')
        self.core['t2']['status']['state'] = 'idle'
        self.shell_path(key)

    def shell_path(self, key):
        # The shipped shell CLI runs in a hermetic fake Herdr HOME/PATH.
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        core = self.home / 'core.json'
        core.write_bytes(runtime.encoded(self.core))
        tool = bin_dir / 'herdr-core'
        tool.write_text('''#!/usr/bin/python3 -B
import json, os, sys
from pathlib import Path
path = Path(os.environ['FAKE_CORE'])
current = json.loads(path.read_bytes())
args = sys.argv[1:]
if args[:2] == ['get', '--json']:
    print(json.dumps(current[args[-1]]))
elif args[:3] == ['read', '--lines', '0']:
    print('')
elif args[:5] == ['send', '--json', '--if', 'idle', 't2']:
    ledger = json.loads(Path(os.environ['FAKE_GOAL']).read_bytes())
    [(key, row)] = ledger['runtime_reflections'].items()
    handoff = row['handoff']
    assert handoff['state'] == 'intent' and key in args[-1] and handoff['id'] in args[-1]
    assert current['t2']['status']['state'] == 'idle'
    current['t2']['turn'] += 1
    current['t2']['status']['state'] = 'working'
    path.write_text(json.dumps(current))
    print(json.dumps({'turn': current['t2']['turn'], 'status': 'idle'}))
else:
    raise SystemExit(70)
''')
        tool.chmod(0o700)
        def cli(actor, *args):
            return subprocess.run([str(ROOT/'herdr-idle-healer.sh'), args[0], self.goal.name, *args[1:]],
                env={'HOME': str(self.home), 'PATH': f'{bin_dir}:/usr/bin:/bin', 'HC_TERMINAL_ID': actor,
                     'FAKE_CORE': str(core), 'FAKE_GOAL': str(self.goal)}, text=True, capture_output=True, timeout=5)
        self.assertEqual(cli('t2', 'reflection-route', key).returncode, 3)
        result = cli('t1', 'reflection-route', key)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        handoff = self.queue()[key]['handoff']
        row = self.queue()[key]
        subject = {name: row[name] for name in ('project_id', 'participant_id', 'assignment_id', 'herdr', 'turn')}
        report = {**subject, 'reflection_id': key, 'handoff_id': handoff['id'],
                  **{field: 'Fixture observation.' for field in reflection.FIELDS},
                  'evidence': [self.artifact('cli-report-log', {'fixture': 'observed'})]}
        proof = self.directory/'cli-result.json'
        proof.write_bytes(runtime.encoded(report))
        self.assertEqual(cli('t1', 'reflection-record', key, '--proof-file', str(proof)).returncode, 3)
        result = cli('t2', 'reflection-record', key, '--proof-file', str(proof))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.queue()[key]['result'], report)
        self.assertEqual(cli('t1', 'reflection-route', key).returncode, 3)


if __name__ == '__main__':
    unittest.main()
