#!/usr/bin/env python3
"""Validate a CoS cycle registry and verify its exact local release candidate."""

import argparse
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(registry, *, registries=()):
    if _versioned(registry):
        graph = build_graph([registry, *registries])
        graph.check_structure(registry.get("project_id"))
        return
    tasks = {task["id"]: task for task in registry["deliverables"]}
    require(len(tasks) == len(registry["deliverables"]), "Duplicate task ID")
    for task in tasks.values():
        for field in ("objective", "evidence", "priority", "dependencies", "owner",
                      "status", "verification", "risk", "decision", "kind"):
            require(field in task, f"{task['id']}: missing {field}")
        require(task["kind"] in {"known", "discovered", "preventative"}, "Invalid task kind")
        require(task["status"] in {"planned", "in_progress", "implemented", "verified",
                                   "integrated", "deferred", "removed"}, "Invalid lifecycle status")
        level = task["decision"]["level"]
        require(level in (1, 2, 3, 4), "Invalid authority level")
        if level >= 3:
            require(task["state"] in {"deferred", "removed"} and
                    task["status"] in {"deferred", "removed"} and task.get("reason"),
                    f"{task['id']}: restricted work cannot run under this charter")
        require(set(task["dependencies"]) <= tasks.keys(), f"{task['id']}: unknown dependency")
        if task["status"] in {"verified", "integrated"}:
            require(task["evidence"] and all(Path(p).is_file() for p in task["evidence"]),
                    f"{task['id']}: acceptance evidence missing")
    visiting = set()
    visited = set()

    def visit(task_id):
        require(task_id not in visiting, f"Dependency cycle: {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["dependencies"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def dependency_status(registry, *, registries=(), observed=None, holds=()):
    if _versioned(registry):
        graph = build_graph([registry, *registries], observed, holds)
        project = registry.get("project_id")
        return graph.view(project_id=project) if _id(project) else []
    tasks = {task["id"]: task for task in registry["deliverables"]}
    result = []
    for task in tasks.values():
        waiting = [
            dependency for dependency in task["dependencies"]
            if tasks[dependency]["status"] not in {"verified", "integrated"}
            or not tasks[dependency]["evidence"]
            or not all(Path(path).is_file() and Path(path).stat().st_size
                       for path in tasks[dependency]["evidence"])
        ]
        result.append({
            "id": task["id"], "objective": task["objective"], "owner": task["owner"],
            "status": task["status"], "dependencies": task["dependencies"],
            "waiting_on": waiting, "evidence": task["evidence"],
            "ready": task["status"] in {"planned", "in_progress", "implemented"}
            and task["decision"]["level"] in (1, 2) and not waiting,
        })
    return result


def verify(registry, receipt=None, *, registries=()):
    if _versioned(registry):
        graph = build_graph([registry, *registries])
        graph.verify_project(registry.get("project_id"), receipt)
        return
    require(all(t["status"] in {"verified", "integrated", "deferred", "removed"}
                for t in registry["deliverables"]), "Cycle has unfinished tasks")
    for node in dependency_status(registry):
        if node["status"] in {"verified", "integrated"}:
            require(not node["waiting_on"], f"{node['id']}: dependencies lack verified evidence")
    candidate = registry["candidate"]
    sha = candidate["sha"]
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Expected exact Git SHA")
    checkout = candidate["checkout"]
    def git(*args):
        return subprocess.check_output(["git", "-C", checkout, *args], text=True).strip()
    require(git("rev-parse", "HEAD") == sha and not git("status", "--porcelain"),
            "Candidate checkout differs from reviewed commit")
    require(receipt["sha"] == sha and receipt["verdict"] == "PASS", "Independent verdict missing")
    require(receipt["reviewer"] == registry["reviewer"] and receipt["reviewer"] not in candidate["makers"],
            "Expected reviewer independent of candidate makers")
    checks = {c["name"]: c for c in receipt["checks"]}
    require(len(checks) == len(receipt["checks"]), "Duplicate check name")
    required = registry["required_checks"]
    require(required and set(required) <= checks.keys(), "Required checks missing")
    for name in required:
        check = checks[name]
        log = Path(check["log"])
        require(check["exit_code"] == 0 and check["status"] == "passed", f"{name}: failed or skipped")
        require(log.is_absolute() and log.is_file() and log.stat().st_size > 0 and
                hashlib.sha256(log.read_bytes()).hexdigest() == check["sha256"],
                f"{name}: log missing or changed")


_STATUSES = {"planned", "in_progress", "implemented", "verified", "integrated", "deferred", "removed"}
_FINAL = {"verified", "integrated"}
_STAGES = ("dispatched", "started", "implemented")


def _versioned(registry):
    return isinstance(registry, dict) and "schema_version" in registry and not (
        type(registry["schema_version"]) is int and registry["schema_version"] == 1)


def _id(value):
    return isinstance(value, str) and bool(value) and value.strip() == value and not any(
        ord(char) < 32 or ord(char) == 127 for char in value)


def _record(value):
    require(isinstance(value, dict), "expected object")
    return value


def _array(value):
    require(isinstance(value, list), "expected array")
    return value


def _sha(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _path(value):
    return isinstance(value, str) and "\x00" not in value and Path(value).is_absolute()


def _held(record):
    value = record.get("held", False)
    require(type(value) is bool, "held must be boolean")
    return value


def _herdr(record):
    record = _record(record)
    terminal, created = record.get("terminal_id"), record.get("created_ms")
    require(_id(terminal) and type(created) is int and created > 0, "invalid Herdr identity")
    return terminal, created


def _artifact(value):
    value = _record(value)
    require(_id(value.get("name")) and _path(value.get("path")) and _sha(value.get("sha256"), 64),
            "invalid artifact identity")
    return value


def attempt_id(project_id, task_id, number):
    require(_id(project_id) and _id(task_id) and type(number) is int and number > 0,
            "invalid attempt identity fields")
    payload = json.dumps([project_id, task_id, number], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _proof(value, attempt, *, current=False):
    value = _artifact(value)
    require(value.get("attempt_id") == attempt["id"] and value.get("scope") == attempt["scope"] and
            _herdr(value.get("herdr")) == _herdr(attempt["herdr"]), "proof does not bind attempt, scope and Herdr identity")
    if current:
        require(_bytes_match(value["path"], value["sha256"]), "proof is missing, empty, or changed")
    return value


def _manifest(value):
    artifacts = [_artifact(item) for item in _array(value)]
    require(len({item["name"] for item in artifacts}) == len(artifacts), "duplicate artifact name")
    return artifacts


def _identity(value):
    value = _record(value)
    if value.get("kind") == "git":
        require(_path(value.get("checkout")) and _sha(value.get("sha"), 40), "invalid Git identity")
    else:
        require(value.get("kind") == "artifact" and _path(value.get("path")) and _sha(value.get("sha256"), 64),
                "invalid source or work identity")
    return value


def _bytes_match(path, sha):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            digest = hashlib.sha256()
            size = 0
            for block in iter(lambda: stream.read(1024 * 128), b""):
                size += len(block)
                digest.update(block)
            return size > 0 and digest.hexdigest() == sha
    except (OSError, ValueError):
        return False


def _identity_current(value, *, source=False):
    if value["kind"] == "artifact":
        return _bytes_match(value["path"], value["sha256"])
    checkout, sha = value["checkout"], value["sha"]
    env = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    def git(*args):
        return subprocess.check_output(["git", "-c", "core.fsmonitor=false", "-C", checkout, *args], env=env,
                                       stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
    try:
        if Path(git("rev-parse", "--show-toplevel")).resolve() != Path(checkout).resolve():
            return False
        if source:
            return git("cat-file", "-t", sha) == "commit"
        return git("rev-parse", "HEAD") == sha and not git("status", "--porcelain", "--untracked-files=all")
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _edge(value):
    record = _record(value)
    project, task = record.get("project_id"), record.get("task_id")
    require(_id(project) and _id(task), "invalid dependency identity")
    return project, task


def _edge_record(key):
    return {"project_id": key[0], "task_id": key[1]}


def _cycle_nodes(edges):
    """Iterative SCC detection so graph depth isn't bounded by Python recursion."""
    reverse = {key: [] for key in edges}
    for key, deps in edges.items():
        for dep in deps:
            reverse[dep].append(key)
    seen, order = set(), []
    for key in edges:
        stack = [(key, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((dep, False) for dep in edges[node])
    seen, cycles = set(), set()
    for key in reversed(order):
        component, stack = [], [key]
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                component.append(node)
                stack.extend(reverse[node])
        if len(component) > 1 or component and key in edges[key]:
            cycles.update(component)
    return cycles


class Graph:
    """One checked registry snapshot; persistence and Herdr observations are external."""

    def __init__(self, registries, observed=None, holds=()):
        self.errors = []
        self._structural_projects = set()
        self._projects = {}
        self._participants = {}
        self._paths = {}
        self._nodes = {}
        self._edges = {}
        self._invalid_projects = set()
        self._blockers = defaultdict(set)
        self._observed = dict(observed or {})
        self._holds = set(holds)
        self._registries = deepcopy(list(registries))
        self._read_projects(self._registries)
        for key, task in self._nodes.items():
            if key[0] in self._invalid_projects:
                self._blockers[key].add("malformed")
                continue
            try:
                self._check_task(key, task)
            except (KeyError, TypeError, ValueError) as error:
                self._malformed(key, str(error))
        self._active_handles, self._active_scopes = self._collect_active_resources()
        known_edges = {key: [dep for dep in self._edges.get(key, ()) if dep in self._nodes]
                       for key in self._nodes}
        for key, edges in self._edges.items():
            for dep in edges:
                if dep not in self._nodes:
                    self._malformed(key, f"unknown dependency {dep[0]}/{dep[1]}", "unknown_dependency")
        cycles = _cycle_nodes(known_edges)
        for key in sorted(cycles):
            self._malformed(key, "dependency cycle", "dependency_cycle")
        self._accepted = self._collect_acceptance(known_edges)

    def _issue(self, location, code, message):
        self.errors.append({"location": location, "code": code, "message": message})

    def _project_error(self, project, message, *, invalidate=False):
        self._structural_projects.add(project)
        if invalidate:
            self._invalid_projects.add(project)
        self._issue(project, "malformed", message)

    def _malformed(self, key, message, blocker="malformed"):
        self._structural_projects.add(key[0])
        self._blockers[key].update(("malformed", blocker))
        self._issue("/".join(key), "malformed", message)

    def _stale(self, key, message):
        self._blockers[key].add("stale_evidence")
        self._issue("/".join(key), "stale_evidence", message)

    def _read_projects(self, registries):
        for index, value in enumerate(registries):
            if not isinstance(value, dict):
                self._issue(f"registry[{index}]", "malformed", "expected registry object")
                continue
            if not _versioned(value):
                continue
            project = value.get("project_id")
            if not _id(project):
                self._issue(f"registry[{index}]", "malformed", "invalid project identity")
                continue
            if project in self._projects:
                self._project_error(project, "duplicate project identity", invalidate=True)
                continue
            registry = self._projects[project] = value
            try:
                require(type(registry.get("schema_version")) is int and registry["schema_version"] == 2,
                        "unsupported schema_version")
                _held(registry)
                _array(registry.get("deliverables"))
                self._read_participants(project, registry)
            except (KeyError, TypeError, ValueError) as error:
                self._project_error(project, str(error), invalidate=True)
            for task in registry.get("deliverables", []) if isinstance(registry.get("deliverables"), list) else ():
                if not isinstance(task, dict) or not _id(task.get("id")):
                    self._project_error(project, "invalid task identity")
                    continue
                key = project, task["id"]
                if key in self._nodes:
                    self._malformed(key, "duplicate task identity")
                else:
                    self._nodes[key] = task

    def _read_participants(self, project, registry):
        people, handles, invalid = {}, {}, set()
        coordinator = registry.get("coordinator")
        require(_id(coordinator), "invalid coordinator identity")
        for record in _array(registry.get("participants")):
            person = record.get("id") if isinstance(record, dict) else None
            if not _id(person):
                self._project_error(project, "invalid participant identity")
                continue
            if person in people:
                invalid.add(person)
                self._project_error(project, f"duplicate participant {person}")
                continue
            people[person] = record
            try:
                require(_id(record.get("role")), "invalid role")
                require("parent" in record and (record["parent"] is None or _id(record["parent"])),
                        "invalid parent identity")
                handle = _herdr(record.get("herdr"))
                if handle in handles:
                    invalid.add(handles[handle])
                    raise ValueError("duplicate Herdr handle")
                handles[handle] = person
            except ValueError as error:
                invalid.add(person)
                self._project_error(project, f"participant {person}: {error}")
        require(coordinator in people and coordinator not in invalid and people[coordinator]["parent"] is None,
                "missing or invalid coordinator root")
        paths = {coordinator: (coordinator,)}
        for person in people:
            cursor, chain, seen = person, [], set()
            while cursor not in paths and cursor in people and cursor not in invalid and cursor not in seen:
                seen.add(cursor)
                chain.append(cursor)
                cursor = people[cursor]["parent"]
            if cursor in paths:
                for child in reversed(chain):
                    paths[child] = paths[cursor] + (child,)
                    cursor = child
            else:
                invalid.update(chain)
        for person in people.keys() - paths.keys():
            self._project_error(project, f"participant {person}: invalid nesting or Herdr identity")
        self._participants[project] = people
        self._paths[project] = paths

    def _collect_active_resources(self):
        handles, scopes = defaultdict(set), defaultdict(set)
        for key, task in self._nodes.items():
            attempts = task.get("attempts")
            if not isinstance(attempts, list):
                continue
            for attempt in attempts:
                if isinstance(attempt, dict) and attempt.get("state") == "active":
                    try:
                        handles[_herdr(attempt.get("herdr"))].add(key)
                    except ValueError:
                        pass  # Unidentifiable owner is already structurally blocked.
                    scope = attempt.get("scope")
                    if _id(scope):
                        scopes[scope].add(key)
        for index, resource, blocker in ((handles, "Herdr handle", "owner_busy"),
                                         (scopes, "scope key", "scope_busy")):
            for keys in index.values():
                if len(keys) > 1:
                    for key in sorted(keys):
                        self._malformed(key, f"{resource} has multiple active attempts", blocker)
        return handles, scopes

    def _check_attempts(self, project, task):
        attempts = _array(task.get("attempts"))
        last_owner = None
        proof_hashes = set()
        for number, attempt in enumerate(attempts, 1):
            attempt = _record(attempt)
            require(type(attempt.get("number")) is int and attempt["number"] == number and
                    attempt.get("id") == attempt_id(project, task["id"], number), "invalid attempt number or ID")
            require(attempt.get("scope") == task["scope"], "attempt scope differs from authorized task scope")
            require(_identity(attempt.get("source")) == task["source"], "attempt source differs from pinned task source")
            owner = attempt.get("owner")
            require(_id(owner) and owner in self._paths[project] and owner != last_owner,
                    "attempt owner missing or unchanged after reassignment")
            last_owner = owner
            _herdr(attempt.get("herdr"))
            state = attempt.get("state")
            require(state == "settled" or state == "active" and number == len(attempts),
                    "only final attempt can be active")
            proofs = []
            if state == "settled":
                proofs.append(_proof(attempt.get("settlement"), attempt))
            else:
                require("settlement" not in attempt and owner == task["owner"] and
                        _herdr(attempt["herdr"]) == _herdr(self._participants[project][owner]["herdr"]),
                        "active attempt is not bound to current owner and Herdr identity")
            checkpoints = _array(attempt.get("checkpoints"))
            require(1 <= len(checkpoints) <= len(_STAGES), "invalid checkpoint count")
            for stage, checkpoint in zip(_STAGES, checkpoints):
                checkpoint = _record(checkpoint)
                require(checkpoint.get("stage") == stage, "checkpoint skipped or out of order")
                proofs.append(_proof(checkpoint.get("evidence"), attempt))
                if stage == "implemented":
                    _identity(checkpoint.get("work"))
                    _manifest(checkpoint.get("artifacts"))
            for proof in proofs:
                require(proof["sha256"] not in proof_hashes, "lifecycle proof cannot be replayed")
                proof_hashes.add(proof["sha256"])
        return attempts

    def _check_task(self, key, task):
        project = key[0]
        require(isinstance(task.get("objective"), str) and bool(task["objective"].strip()), "missing objective")
        dependencies = [_edge(edge) for edge in _array(task.get("dependencies"))]
        require(len(set(dependencies)) == len(dependencies), "duplicate dependency")
        self._edges[key] = dependencies
        owner = task.get("owner")
        require(_id(owner) and owner in self._paths[project], "missing or invalid task owner")
        _held(task)
        require(_id(task.get("scope")), "missing authorized scope reference")
        level = task.get("level")
        require(type(level) is int and level in (1, 2, 3, 4), "invalid authority level")
        status = task.get("status")
        require(isinstance(status, str) and status in _STATUSES, "invalid status")
        source = _identity(task.get("source"))
        required = _array(task.get("required_artifacts"))
        require(required and all(_id(name) for name in required) and len(set(required)) == len(required),
                "required artifact names must be unique and nonempty")
        attempts = self._check_attempts(project, task)
        last = attempts[-1] if attempts else None
        stage = last["checkpoints"][-1]["stage"] if last else None
        if status == "planned":
            require(not last or last["state"] == "settled" and last["owner"] != owner,
                    "planned task has not been reassigned after settlement")
        if status in {"in_progress", "implemented", *_FINAL}:
            require(level in (1, 2), "restricted task cannot have actionable status")
            require(last and last["owner"] == owner, "running or implemented task lacks owner-bound attempt")
        if status == "in_progress":
            require(stage != "implemented", "in-progress task has an implemented checkpoint")
        if status in {"deferred", "removed"}:
            require(not last or last["state"] == "settled", "closed task has an active attempt")
        work = _identity(task["work"]) if "work" in task else None
        artifacts = _manifest(task.get("artifacts", []))
        if status in {"implemented", *_FINAL}:
            require(stage == "implemented" and work and set(required) <= {item["name"] for item in artifacts},
                    "implementation lacks checkpoint, work identity, or required artifacts")
            checkpoint = last["checkpoints"][-1]
            require(work == checkpoint["work"] and artifacts == checkpoint["artifacts"],
                    "implementation differs from recorded checkpoint")
        elif status in {"planned", "in_progress"}:
            require(not work and not artifacts, "task has outputs before implementation")
        if status in _FINAL:
            require(last["state"] == "settled", "accepted task has unsettled writer")
            self._check_review(project, task, attempts)
        else:
            require("review" not in task, "review requires verified or integrated status")
        if not _identity_current(source, source=True):
            self._stale(key, "source identity missing or changed")
        if work and not _identity_current(work):
            self._stale(key, "work identity missing, changed, or dirty")
        for attempt in attempts:
            proofs = [item["evidence"] for item in attempt["checkpoints"]]
            if attempt["state"] == "settled":
                proofs.append(attempt["settlement"])
            for proof in proofs:
                self._check_bytes(key, proof, f"attempt {attempt['number']}")
        for item in artifacts:
            self._check_bytes(key, item, "deliverable")

    def _check_review(self, project, task, attempts):
        review = _record(task.get("review"))
        reviewer = review.get("reviewer")
        require(_id(reviewer) and reviewer in self._paths[project] and
                reviewer not in {attempt["owner"] for attempt in attempts} and
                _herdr(self._participants[project][reviewer]["herdr"]) not in
                {_herdr(attempt["herdr"]) for attempt in attempts}, "review is not independent")
        require(review.get("verdict") == "PASS" and type(review.get("attempt")) is int and
                review["attempt"] == attempts[-1]["number"], "review does not pass final attempt")
        _identity(review.get("source"))
        _identity(review.get("work"))
        _manifest(review.get("artifacts"))
        require(review["source"] == task["source"] and review["work"] == task["work"] and
                review["artifacts"] == task["artifacts"], "review does not bind exact source, work and artifacts")

    def _check_bytes(self, key, proof, label):
        if not _bytes_match(proof["path"], proof["sha256"]):
            self._stale(key, f"{label} artifact {proof['name']} missing, empty, or changed")

    def _is_held(self, key):
        return (self._projects[key[0]].get("held") is True or
                self._nodes[key].get("held") is True or key in self._holds)

    def _fresh(self, project, owner):
        if not _id(owner) or owner not in self._paths.get(project, {}):
            return False
        terminal, created = _herdr(self._participants[project][owner]["herdr"])
        value = self._observed.get(terminal)
        return type(value) is int and value == created

    def _collect_acceptance(self, edges):
        candidates = {key for key, task in self._nodes.items()
                      if isinstance(task.get("status"), str) and task["status"] in _FINAL
                      and not self._blockers[key] and not self._is_held(key)}
        unmet = {key: len(edges[key]) for key in candidates}
        reverse = defaultdict(list)
        for key in candidates:
            for dependency in edges[key]:
                reverse[dependency].append(key)
        ready = deque(key for key, count in unmet.items() if not count)
        accepted = set()
        while ready:
            key = ready.popleft()
            accepted.add(key)
            for dependent in reverse[key]:
                unmet[dependent] -= 1
                if not unmet[dependent]:
                    ready.append(dependent)
        return accepted

    def _waiting(self, key):
        return [dep for dep in self._edges.get(key, ()) if dep not in self._accepted]

    def view(self, project_id=None, role=None, participant_id=None, task_id=None):
        result = []
        for key, task in sorted(self._nodes.items()):
            project, task_key = key
            owner = task.get("owner") if _id(task.get("owner")) else None
            participant = self._participants.get(project, {}).get(owner, {})
            owner_role = participant.get("role") if _id(participant.get("role")) else None
            if any(value is not None and value != actual for value, actual in
                   ((project_id, project), (role, owner_role), (participant_id, owner), (task_id, task_key))):
                continue
            blockers = set(self._blockers[key])
            status = task.get("status")
            waiting = self._waiting(key)
            held = self._is_held(key)
            if waiting:
                blockers.add("waiting_on_dependencies")
            if held:
                blockers.add("held")
            if type(task.get("level")) is not int or task["level"] not in (1, 2):
                blockers.add("restricted")
            if status != "planned":
                blockers.add("not_planned")
            elif not self._fresh(project, owner):
                blockers.add("owner_not_observed")
            elif self._active_handles.get(_herdr(participant["herdr"])):
                blockers.add("owner_busy")
            if status == "planned" and _id(task.get("scope")) and self._active_scopes.get(task["scope"]):
                blockers.add("scope_busy")
            result.append({
                "project_id": project, "task_id": task_key, "objective": task.get("objective"),
                "owner": owner, "role": owner_role,
                "participant_path": list(self._paths.get(project, {}).get(owner, ())),
                "scope": task.get("scope"), "status": status, "level": task.get("level"), "held": held,
                "dependencies": [_edge_record(dep) for dep in self._edges.get(key, ())],
                "waiting_on": [_edge_record(dep) for dep in waiting],
                "ready": not blockers, "accepted": key in self._accepted,
                "blockers": sorted(blockers), "attempts": deepcopy(task.get("attempts", [])),
            })
        return result

    def transition(self, project_id, task_id, event, actor, **payload):
        require(_id(project_id) and _id(task_id), "invalid project/task identity")
        key = project_id, task_id
        require(key in self._nodes and "malformed" not in self._blockers[key], "task missing or malformed")
        require(_id(actor) and actor in self._paths[project_id], "actor must be a registered participant")
        registry = deepcopy(self._projects[project_id])
        task = next(task for task in registry["deliverables"] if isinstance(task, dict) and task.get("id") == task_id)
        coordinator = registry["coordinator"]
        attempts = task["attempts"]
        active = attempts[-1] if attempts and attempts[-1]["state"] == "active" else None
        held = self._is_held(key)
        owner = task["owner"]
        if event == "dispatch":
            require(actor == coordinator, "only coordinator may dispatch")
            require(self.view(project_id=project_id, task_id=task_id)[0]["ready"], "task is not ready")
            number = len(attempts) + 1
            attempt = {"id": attempt_id(*key, number), "number": number, "owner": owner,
                       "herdr": deepcopy(self._participants[project_id][owner]["herdr"]),
                       "scope": task["scope"], "source": deepcopy(task["source"]),
                       "state": "active", "checkpoints": []}
            proof = _proof(payload.get("evidence"), attempt, current=True)
            attempt["checkpoints"].append({"stage": "dispatched", "evidence": deepcopy(proof)})
            attempts.append(attempt)
            task["status"] = "in_progress"
        elif event == "checkpoint":
            require(active and actor == owner and self._fresh(project_id, owner),
                    "checkpoint requires observed active owner")
            require(not held and not self._blockers[key] and not self._waiting(key), "checkpoint is blocked")
            stage = payload.get("stage")
            index = len(active["checkpoints"])
            require(index < len(_STAGES) and stage == _STAGES[index], "checkpoint skipped or out of order")
            proof = _proof(payload.get("evidence"), active, current=True)
            active["checkpoints"].append({"stage": stage, "evidence": deepcopy(proof)})
            if stage == "implemented":
                task["work"] = deepcopy(_identity(payload.get("work")))
                task["artifacts"] = deepcopy(_manifest(payload.get("artifacts")))
                active["checkpoints"][-1].update(work=deepcopy(task["work"]), artifacts=deepcopy(task["artifacts"]))
                task["status"] = "implemented"
        elif event == "settle":
            require(active and actor in (owner, coordinator), "settlement requires active owner or coordinator")
            proof = _proof(payload.get("evidence"), active, current=True)
            active["settlement"] = deepcopy(proof)
            active["state"] = "settled"
        elif event == "reassign":
            require(actor == coordinator and not held, "reassignment requires coordinator and no hold")
            require(attempts and not active and task["status"] in {"in_progress", "implemented"},
                    "reassignment requires a settled, unfinished attempt")
            _proof(attempts[-1]["settlement"], attempts[-1], current=True)
            new_owner = payload.get("owner")
            require(new_owner != owner and self._fresh(project_id, new_owner) and
                    _herdr(self._participants[project_id][new_owner]["herdr"]) != _herdr(attempts[-1]["herdr"]),
                    "new owner must have a distinct observed Herdr identity")
            task["owner"] = new_owner
            task["status"] = "planned"
            task.pop("work", None)
            task.pop("artifacts", None)
        elif event == "accept":
            review = _record(payload.get("review"))
            require(actor == review.get("reviewer"), "actor must be the reviewing participant")
            require(task["status"] == "implemented" and not active, "acceptance requires settled implementation")
            require(not held and not self._blockers[key] and not self._waiting(key), "acceptance is blocked")
            task["review"] = deepcopy(review)
            task["status"] = "verified"
        elif event == "integrate":
            require(actor == coordinator and task["status"] == "verified" and key in self._accepted,
                    "integration requires coordinator and accepted verification")
            task["status"] = "integrated"
        else:
            raise ValueError("unsupported transition")
        records = [registry if value is self._projects[project_id] else value for value in self._registries]
        checked = build_graph(records, self._observed, self._holds)
        require("malformed" not in checked._blockers[key], "transition produced malformed task")
        if event in {"dispatch", "checkpoint"}:
            require("stale_evidence" not in checked._blockers[key], "transition evidence is stale")
        if event in {"accept", "integrate"}:
            require(key in checked._accepted, "work is not verified against current evidence and prerequisites")
        return registry

    def check_structure(self, project):
        require(_id(project) and project in self._projects, "missing or invalid v2 project")
        require(project not in self._structural_projects, f"{project}: malformed registry (inspect graph.errors)")

    def verify_project(self, project, receipt=None):
        self.check_structure(project)
        tasks = [key for key in self._nodes if key[0] == project]
        require(tasks, "project has no deliverables")
        require(all(self._nodes[key]["status"] in {*_FINAL, "deferred", "removed"} for key in tasks),
                "project has unfinished tasks")
        reviews = {key[1]: self._nodes[key]["review"] for key in tasks if self._nodes[key]["status"] in _FINAL}
        require(all((project, key) in self._accepted for key in reviews), "project has unaccepted task evidence")
        if receipt is not None:
            require(receipt == {"project_id": project, "reviews": reviews}, "independent receipt differs from graph")


def build_graph(registries, observed=None, holds=()):
    return Graph(registries, observed, holds)


def _distinct_keys(pairs):
    record = {}
    for key, value in pairs:
        require(key not in record, "duplicate JSON member")
        record[key] = value
    return record


def _goal_files(goals_dir, *, exclude=None):
    records, errors = [], []
    for path in sorted(Path(goals_dir).glob("*.json")):
        try:
            if exclude is not None and path.resolve() == exclude:
                continue
            records.append(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_distinct_keys))
        except (OSError, ValueError, RuntimeError) as error:
            errors.append((str(path), "malformed", str(error)))
    return records, errors


def load_graph(goals_dir, observed=None, holds=()):
    records, errors = _goal_files(goals_dir)
    graph = build_graph(records, observed, holds)
    for error in errors:
        graph._issue(*error)
    return graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--goals-dir", type=Path, help="Load v2 peer registries from this goals directory.")
    parser.add_argument("--graph", action="store_true", help="Show owners, dependencies, readiness, and evidence.")
    args = parser.parse_args()
    try:
        registry = json.loads(args.registry.read_text())
        peers = []
        version = registry.get("schema_version") if isinstance(registry, dict) else None
        v2 = type(version) is int and version == 2
        goals_dir = args.goals_dir
        parent = args.registry.absolute().parent
        if goals_dir is None and v2 and parent.name == "goals":
            goals_dir = parent
        if goals_dir is not None:
            require(v2 and goals_dir.is_dir(), "--goals-dir requires a v2 registry and a directory")
            peers, _ = _goal_files(goals_dir, exclude=args.registry.resolve())
        validate(registry, registries=peers)
        if args.receipt:
            receipt = json.loads(args.receipt.read_text())
            require(isinstance(receipt, dict), "--receipt must contain a JSON object")
            verify(registry, receipt, registries=peers)
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Gate failed: {error}\n")
    if args.graph:
        print(json.dumps(dependency_status(registry, registries=peers), indent=2))
    else:
        print("Candidate evidence passed." if args.receipt else "Registry valid.")


if __name__ == "__main__":
    main()
