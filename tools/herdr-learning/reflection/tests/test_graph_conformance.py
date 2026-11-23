"""Black-box UO-02 v2 contract and frozen legacy gate compatibility (stdlib only)."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('UO_SOURCE', str(Path(__file__).resolve().parents[1])))
import herdr_cycle_gate as gate

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def read_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ref(project, task):
    return {'project_id': project, 'task_id': task}


class LegacyCompatibility(unittest.TestCase):
    def test_a2_dual_lifecycle_and_exact_independent_receipt(self):
        original = read_fixture('legacy-a2-independent.json')
        self.assertNotIn('schema_version', original)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            files = {key: root / key for key in ('implementation', 'db', 'closeout', 'receipt')}
            for key, path in files.items():
                path.write_text(f'synthetic {key} evidence\n', encoding='utf-8')

            def materialize(value):
                if isinstance(value, dict):
                    return {key: materialize(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [materialize(item) for item in value]
                if isinstance(value, str) and value.startswith('@'):
                    return str(root if value == '@checkout' else files[value[1:]])
                return value

            ledger = materialize(original)
            snapshot = copy.deepcopy(ledger)
            checks = [{'name': name, 'log': str(files[key]), 'status': 'passed', 'exit_code': 0,
                       'sha256': digest(files[key])}
                      for name, key in [('focused', 'implementation'), ('disposable-db', 'db')]]
            sha = ledger['candidate']['sha']
            receipt = {'sha': sha, 'verdict': 'PASS', 'reviewer': ledger['reviewer'], 'checks': checks}

            def fake_git(command, **kwargs):
                self.assertEqual(command[:3], ['git', '-C', str(root)])
                self.assertEqual(kwargs, {'text': True})
                return {('rev-parse', 'HEAD'): sha + '\n', ('status', '--porcelain'): ''}[tuple(command[3:])]

            with patch.object(gate.subprocess, 'check_output', side_effect=fake_git):
                gate.validate(ledger)
                self.assertEqual([row['ready'] for row in gate.dependency_status(ledger)], [False] * 3)
                gate.verify(ledger, receipt)
                files['db'].write_text('synthetic after verification tamper\n', encoding='utf-8')
                with self.assertRaises(ValueError):
                    gate.verify(ledger, receipt)
            self.assertEqual(ledger, snapshot)

    def test_legacy_report_completed_without_transport_but_empty_report_needs_reconcile(self):
        shell = Path(gate.__file__).resolve().with_name('herdr-idle-healer.sh')
        body = shell.read_text(encoding='utf-8').split("<<'PY'\n", 1)[1].rsplit('\nPY\n', 1)[0]
        namespace = {'__name__': 'conformance_legacy_healer'}
        with patch.dict(os.environ, {'HERDR_HEALER_LIB_DIR': str(shell.parent)}):
            exec(compile(body, str(shell), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = read_fixture('legacy-report-closeout.json')
            closeout, task_report, goal = root / 'closeout', root / 'report', root / 'goal.json'
            ledger['closeout_evidence'] = str(closeout)
            ledger['deliverables'][0]['evidence'] = [str(task_report)]
            task_report.write_bytes(b'synthetic task evidence\n')
            closeout.write_bytes(b'synthetic goal closeout\n')
            goal.write_text(json.dumps(ledger), encoding='utf-8')
            before = goal.read_bytes()
            with patch.dict(namespace, {'herdr': lambda *a: self.fail(f'unexpected transport {a!r}')}):
                self.assertIsNone(namespace['snapshot'](goal))
                closeout.write_bytes(b'')
                self.assertEqual(namespace['closeout_failure'](ledger), 'Closeout report missing.')
            self.assertEqual(goal.read_bytes(), before)


class GraphFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.counter = 0
        self.registries = [self.project('product', 100), self.project('handbook', 200)]
        self.observed = {participant['herdr']['terminal_id']: participant['herdr']['created_ms']
                         for project in self.registries for participant in project['participants']}
        self.holds = []
        self.add_task('product', 'backend', 'backend')
        self.add_task('product', 'frontend', 'frontend', [ref('product', 'backend')])
        self.add_task('product', 'qa', 'qa', [ref('product', 'frontend')])
        self.add_task('product', 'review-interface', 'security', [ref('product', 'backend')])
        self.add_task('handbook', 'backend', 'docs', [ref('product', 'backend')])
        self.add_task('handbook', 'publish-guide', 'docs', [ref('product', 'qa'), ref('handbook', 'backend')])
        self.add_task('handbook', 'unrelated', 'docs')

    def project(self, project_id, seed):
        rows = [('cos', None, 'coordinator'), ('backend', 'cos', 'Backend'),
                ('frontend', 'cos', 'Frontend'), ('qa', 'frontend', 'QA'),
                ('security', 'qa', 'Security'), ('docs', 'cos', 'Documentation'),
                ('reviewer', 'cos', 'Independent acceptance'), ('alternate', 'cos', 'Additional maker')]
        return {'schema_version': 2, 'project_id': project_id, 'coordinator': 'cos', 'held': False,
                'participants': [{'id': who, 'parent': parent, 'role': role,
                                  'herdr': {'terminal_id': f't{seed+i}', 'created_ms': seed*100+i}}
                                 for i, (who, parent, role) in enumerate(rows)], 'deliverables': []}

    def artifact(self, name):
        self.counter += 1
        path = self.root / f'{self.counter:02d}-{name}.txt'
        path.write_text(f'{self.counter} fixture {name}\n', encoding='utf-8')
        return {'name': name, 'path': str(path), 'sha256': digest(path)}

    def identity(self, name):
        artifact = self.artifact(name)
        return {'kind': 'artifact', 'path': artifact['path'], 'sha256': artifact['sha256']}

    def registry(self, project):
        return next(item for item in self.registries if item['project_id'] == project)

    def task(self, project, task):
        return next(item for item in self.registry(project)['deliverables'] if item['id'] == task)

    def add_task(self, project, name, owner, dependencies=()):
        self.registry(project)['deliverables'].append({
            'id': name, 'objective': f'Synthetic {name}', 'owner': owner, 'scope': f'fixture:{project}/{name}',
            'level': 1, 'held': False,
            'status': 'planned', 'dependencies': list(dependencies), 'source': self.identity(name),
            'required_artifacts': ['result'], 'attempts': [],
        })

    def graph(self):
        return gate.build_graph(self.registries, observed=self.observed, holds=self.holds)

    def row(self, project, task):
        [row] = self.graph().view(project_id=project, task_id=task)
        return row

    def transition(self, project, task, event, who, **payload):
        before = copy.deepcopy(self.registries)
        try:
            changed = self.graph().transition(project, task, event, who, **payload)
        finally:
            self.assertEqual(self.registries, before, f'{event} mutated source ledger')
        self.registries[self.registries.index(self.registry(project))] = changed

    def proof(self, name, project, task):
        row = self.task(project, task)
        if name == 'dispatch':
            number = len(row['attempts']) + 1
            herdr = next(p['herdr'] for p in self.registry(project)['participants'] if p['id'] == row['owner'])
        else:
            attempt = row['attempts'][-1]
            number, herdr = attempt['number'], attempt['herdr']
        return {**self.artifact(name), 'attempt_id': gate.attempt_id(project, task, number),
                'scope': row['scope'], 'herdr': copy.deepcopy(herdr)}

    def dispatch(self, project, task, by='cos'):
        self.transition(project, task, 'dispatch', by, evidence=self.proof('dispatch', project, task))

    def implement(self, project, task, owner):
        self.transition(project, task, 'checkpoint', owner, stage='started', evidence=self.proof('started', project, task))
        self.transition(project, task, 'checkpoint', owner, stage='implemented', evidence=self.proof('implemented', project, task),
                        work=self.identity('work'), artifacts=[self.artifact('result')])

    def settle(self, project, task, owner):
        self.transition(project, task, 'settle', owner, evidence=self.proof('settlement', project, task))

    def review(self, project, task, reviewer='reviewer'):
        row = self.task(project, task)
        return dict(reviewer=reviewer, verdict='PASS', attempt=row['attempts'][-1]['number'],
                    source=copy.deepcopy(row['source']), work=copy.deepcopy(row['work']),
                    artifacts=copy.deepcopy(row['artifacts']))

    def accept(self, project, task, owner):
        self.dispatch(project, task)
        self.implement(project, task, owner)
        self.settle(project, task, owner)
        self.transition(project, task, 'accept', 'reviewer', review=self.review(project, task))


class UniversalGraph(GraphFixture):
    def test_role_neutral_non_git_handoffs_and_nested_views(self):
        self.assertTrue(self.row('product', 'backend')['ready'])
        self.assertFalse(self.row('product', 'frontend')['ready'])
        self.assertFalse(self.row('handbook', 'backend')['ready'])
        [security] = self.graph().view(project_id='product', participant_id='security', role='Security')
        self.assertEqual(security['participant_path'], ['cos', 'frontend', 'qa', 'security'])
        self.assertEqual(len(self.graph().view(task_id='backend')), 2)
        json.dumps(self.graph().view())
        self.accept('product', 'backend', 'backend')
        for project, name in [('product', 'frontend'), ('product', 'review-interface'), ('handbook', 'backend')]:
            self.assertTrue(self.row(project, name)['ready'], name)
        self.assertFalse(self.row('product', 'qa')['ready'])
        self.assertFalse(self.row('handbook', 'publish-guide')['ready'])
        self.transition('product', 'backend', 'integrate', 'cos')
        self.assertEqual(self.row('product', 'backend')['status'], 'integrated')
        self.accept('product', 'frontend', 'frontend')
        self.assertTrue(self.row('product', 'qa')['ready'])
        self.accept('product', 'qa', 'qa')
        self.assertFalse(self.row('handbook', 'publish-guide')['ready'])
        self.accept('handbook', 'backend', 'docs')
        self.assertTrue(self.row('handbook', 'publish-guide')['ready'])

    def test_artifact_tampered_after_acceptance_blocks_consumers_not_unrelated_work(self):
        self.accept('product', 'backend', 'backend')
        self.assertTrue(self.row('product', 'backend')['accepted'])
        path = Path(self.task('product', 'backend')['artifacts'][0]['path'])
        path.write_text('mutated after independent acceptance\n', encoding='utf-8')
        view = self.graph()
        self.assertIn('stale_evidence', {entry['code'] for entry in view.errors})
        self.assertFalse(self.row('product', 'backend')['accepted'])
        for project, task in [('product', 'frontend'), ('product', 'review-interface'), ('handbook', 'backend')]:
            self.assertFalse(self.row(project, task)['ready'])
        with self.assertRaises(ValueError):
            self.dispatch('product', 'frontend')
        self.assertTrue(self.row('handbook', 'unrelated')['ready'])
        self.dispatch('handbook', 'unrelated')

    def test_malformed_cycle_orphans_and_owner_do_not_block_unrelated_dispatch(self):
        self.add_task('product', 'bad-owner', 'unregistered')
        self.add_task('product', 'orphan', 'backend', [ref('missing', 'task')])
        self.add_task('product', 'cycle-a', 'backend', [ref('handbook', 'cycle-b')])
        self.add_task('handbook', 'cycle-b', 'docs', [ref('product', 'cycle-a')])
        self.add_task('product', 'disconnected', 'backend')
        self.add_task('product', 'disconnected-consumer', 'alternate', [ref('product', 'disconnected')])
        graph = self.graph()
        self.assertIn('malformed', {entry['code'] for entry in graph.errors})
        for project, name in [('product', 'bad-owner'), ('product', 'orphan'), ('product', 'cycle-a'), ('handbook', 'cycle-b')]:
            self.assertFalse(self.row(project, name)['ready'], name)
        self.assertTrue(self.row('product', 'disconnected')['ready'])
        self.assertTrue(self.row('handbook', 'unrelated')['ready'])
        self.assertFalse(self.row('product', 'disconnected-consumer')['ready'])
        self.accept('product', 'disconnected', 'backend')
        self.assertTrue(self.row('product', 'disconnected-consumer')['ready'])
        self.dispatch('product', 'disconnected-consumer')
        self.dispatch('handbook', 'unrelated')

    def test_held_project_task_or_external_hold_does_not_consume_ready_peer(self):
        for hold in ('project', 'task', 'external', 'authority'):
            with self.subTest(hold=hold):
                before = copy.deepcopy(self.registries)
                if hold == 'project':
                    self.registry('product')['held'] = True
                elif hold == 'task':
                    self.task('product', 'backend')['held'] = True
                elif hold == 'external':
                    self.holds = [('product', 'backend')]
                else:
                    self.task('product', 'backend')['level'] = 3
                try:
                    self.assertFalse(self.row('product', 'backend')['ready'])
                    with self.assertRaises(ValueError):
                        self.dispatch('product', 'backend')
                    self.assertTrue(self.row('handbook', 'unrelated')['ready'])
                finally:
                    self.registries, self.holds = before, []
        self.assertTrue(self.row('product', 'backend')['ready'])

    def test_changed_or_missing_terminal_identity_and_ownership_are_distinct_from_role(self):
        owner = next(p for p in self.registry('product')['participants'] if p['id'] == 'backend')
        terminal = owner['herdr']['terminal_id']
        created = self.observed.pop(terminal)
        self.assertFalse(self.row('product', 'backend')['ready'])
        self.observed[terminal] = created + 1
        self.assertFalse(self.row('product', 'backend')['ready'])
        with self.assertRaises(ValueError):
            self.dispatch('product', 'backend')
        self.observed[terminal] = created
        self.assertTrue(self.row('product', 'backend')['ready'])
        with self.assertRaises(ValueError):
            self.dispatch('product', 'backend', by='backend')
        self.dispatch('product', 'backend')
        with self.assertRaises(ValueError):
            self.transition('product', 'backend', 'checkpoint', 'frontend', stage='started',
                            evidence=self.proof('started', 'product', 'backend'))

    def test_physical_generation_cannot_be_dispatched_twice_across_projects(self):
        first = next(p for p in self.registry('product')['participants'] if p['id'] == 'backend')
        second = next(p for p in self.registry('handbook')['participants'] if p['id'] == 'docs')
        second['herdr'] = copy.deepcopy(first['herdr'])
        self.assertTrue(self.row('handbook', 'unrelated')['ready'])
        self.dispatch('product', 'backend')
        self.assertFalse(self.row('handbook', 'unrelated')['ready'])
        with self.assertRaises(ValueError):
            self.dispatch('handbook', 'unrelated')
        self.settle('product', 'backend', 'cos')
        self.assertTrue(self.row('handbook', 'unrelated')['ready'])

    def test_reassign_requires_settled_prior_attempt_and_keeps_history(self):
        self.dispatch('product', 'backend')
        self.implement('product', 'backend', 'backend')
        with self.assertRaises(ValueError):
            self.transition('product', 'backend', 'reassign', 'cos', owner='alternate')
        self.settle('product', 'backend', 'cos')
        prior = copy.deepcopy(self.task('product', 'backend')['attempts'][0])
        self.transition('product', 'backend', 'reassign', 'cos', owner='alternate')
        row = self.task('product', 'backend')
        self.assertEqual(row['owner'], 'alternate')
        self.assertEqual(row['attempts'], [prior])
        self.assertFalse(row.get('work'))
        self.assertFalse(row.get('artifacts'))
        self.dispatch('product', 'backend')
        [original, updated] = self.task('product', 'backend')['attempts']
        self.assertEqual(original, prior)
        self.assertEqual((updated['number'], updated['owner'], updated['state']), (2, 'alternate', 'active'))
        self.implement('product', 'backend', 'alternate')
        self.settle('product', 'backend', 'alternate')
        for former_owner in ('backend', 'alternate'):
            with self.assertRaises(ValueError):
                self.transition('product', 'backend', 'accept', former_owner, review=self.review('product', 'backend', former_owner))
        self.transition('product', 'backend', 'accept', 'reviewer', review=self.review('product', 'backend'))
        self.assertTrue(self.row('product', 'backend')['accepted'])

    def test_acceptance_rejects_unsettled_wrong_manifest_and_wrong_actor(self):
        self.dispatch('product', 'backend')
        self.implement('product', 'backend', 'backend')
        review = self.review('product', 'backend')
        with self.assertRaises(ValueError):
            self.transition('product', 'backend', 'accept', 'reviewer', review=review)
        self.settle('product', 'backend', 'backend')
        with self.assertRaises(ValueError):
            self.transition('product', 'backend', 'accept', 'cos', review=review)
        for field, value in [('attempt', 2), ('source', self.identity('wrong-source')), ('work', self.identity('wrong-work')),
                             ('artifacts', [self.artifact('result')])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.transition('product', 'backend', 'accept', 'reviewer', review={**review, field: value})
        self.assertFalse(self.row('product', 'frontend')['ready'])
        self.transition('product', 'backend', 'accept', 'reviewer', review=review)
        self.assertTrue(self.row('product', 'frontend')['ready'])

    def test_logical_alias_on_same_terminal_generation_is_not_independent(self):
        self.dispatch('product', 'backend')
        self.implement('product', 'backend', 'backend')
        self.settle('product', 'backend', 'backend')
        people = {p['id']: p for p in self.registry('product')['participants']}
        people['reviewer']['herdr'] = copy.deepcopy(people['backend']['herdr'])
        with self.assertRaises(ValueError):
            self.transition('product', 'backend', 'accept', 'reviewer', review=self.review('product', 'backend'))
        self.assertFalse(self.row('product', 'frontend')['ready'])

    def test_load_graph_isolates_bad_files_and_ignores_legacy_without_mutation(self):
        goals = self.root / 'goals'
        goals.mkdir()
        paths = [goals / name for name in ('product.json', 'handbook.json', 'legacy.json', 'unparseable.json')]
        for path, doc in zip(paths[:3], [*self.registries, read_fixture('legacy-report-closeout.json')]):
            path.write_text(json.dumps(doc), encoding='utf-8')
        paths[-1].write_text('{', encoding='utf-8')
        before = [digest(path) for path in paths]
        graph = gate.load_graph(goals, observed=self.observed)
        self.assertIn('malformed', {entry['code'] for entry in graph.errors})
        [row] = graph.view(project_id='handbook', task_id='unrelated')
        self.assertTrue(row['ready'])
        self.assertEqual({row['project_id'] for row in graph.view()}, {'product', 'handbook'})
        self.assertEqual([digest(path) for path in paths], before)


class WatchdogCorrections(GraphFixture):
    def keep_tasks(self, project, *keys):
        ledger = self.registry(project)
        ledger['deliverables'] = [task for task in ledger['deliverables'] if task['id'] in keys]

    def save_goals(self):
        goals = self.root / 'goals'
        goals.mkdir()
        for ledger in self.registries:
            path = goals / (ledger['project_id'] + '.json')
            path.write_text(json.dumps(ledger), encoding='utf-8')
        return goals

    def cli(self, ledger, *args):
        return subprocess.run([sys.executable, '-B', gate.__file__, str(ledger), *map(str, args)],
                              cwd=self.root, env={'HOME': str(self.root), 'TMPDIR': str(self.root), 'PATH': '/usr/bin:/bin'},
                              text=True, capture_output=True, timeout=5, check=False)

    def write_receipt(self, project, task):
        receipt = self.root / 'receipt.json'
        review = self.task(project, task)['review']
        receipt.write_text(json.dumps({'project_id': project, 'reviews': {task: review}}), encoding='utf-8')
        return receipt

    def test_explicit_null_receipt_is_not_a_matching_review(self):
        self.keep_tasks('product', 'backend')
        self.registries = [self.registry('product')]
        self.accept('product', 'backend', 'backend')
        ledger = self.save_goals() / 'product.json'
        receipt = self.write_receipt('product', 'backend')
        matching = self.cli(ledger, '--receipt', receipt)
        self.assertEqual(matching.returncode, 0, matching.stderr)
        receipt.write_text('null\n', encoding='utf-8')
        rejected = self.cli(ledger, '--receipt', receipt)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)

    def test_cli_resolves_cross_project_prerequisite_in_same_goals_directory(self):
        self.keep_tasks('product', 'backend')
        self.keep_tasks('handbook', 'backend')
        self.accept('product', 'backend', 'backend')
        self.accept('handbook', 'backend', 'docs')
        goals = self.save_goals()
        ledger, prerequisite = goals / 'handbook.json', goals / 'product.json'
        receipt = self.write_receipt('handbook', 'backend')
        checksums = [digest(path) for path in (ledger, prerequisite, receipt)]
        view = self.cli(ledger, '--graph')
        self.assertEqual(view.returncode, 0, view.stderr)
        [consumer] = [row for row in json.loads(view.stdout) if row['project_id'] == 'handbook' and row['task_id'] == 'backend']
        self.assertEqual(consumer['dependencies'], [ref('product', 'backend')])
        self.assertEqual(consumer['waiting_on'], [])
        self.assertTrue(consumer['accepted'])
        verified = self.cli(ledger, '--receipt', receipt)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual([digest(path) for path in (ledger, prerequisite, receipt)], checksums)
        prerequisite.unlink()
        missing = self.cli(ledger, '--receipt', receipt)
        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)

    def test_same_scope_with_distinct_handles_blocks_until_settlement_only(self):
        self.add_task('handbook', 'overlap', 'docs')
        self.add_task('product', 'unrelated', 'frontend')
        first, second = self.task('product', 'backend'), self.task('handbook', 'overlap')
        second['scope'] = first['scope']
        first_handle = next(p['herdr'] for p in self.registry('product')['participants'] if p['id'] == first['owner'])
        second_handle = next(p['herdr'] for p in self.registry('handbook')['participants'] if p['id'] == second['owner'])
        self.assertNotEqual(first_handle, second_handle)
        self.assertTrue(self.row('handbook', 'overlap')['ready'])
        self.dispatch('product', 'backend')
        self.assertFalse(self.row('handbook', 'overlap')['ready'])
        with self.assertRaises(ValueError):
            self.dispatch('handbook', 'overlap')
        self.assertTrue(self.row('product', 'unrelated')['ready'])
        self.dispatch('product', 'unrelated')
        self.settle('product', 'backend', 'cos')
        self.assertTrue(self.row('handbook', 'overlap')['ready'])
        self.dispatch('handbook', 'overlap')
