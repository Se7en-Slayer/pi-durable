"""Local transport for registered v2 goals; graph transitions stay in the gate."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

OWNER_HOLD = "[[OWNER_DECISION_REQUIRED]]"
RUNTIME_FIELDS = {"runtime_admissions", "runtime_recovery"}


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def lock(directory):
    with (directory / "healer.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def persist(path, before, record):
    # Supported registry writers must share healer.lock. This comparison also
    # detects ordinary out-of-band drift; it is not a CAS against other writers.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".herdr-", delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(encoded(record))
            stream.flush()
            os.fsync(stream.fileno())
            if path.is_symlink() or path.read_bytes() != before:
                raise ValueError("Goal changed before persistence")
            os.replace(tmp, path)
            sync_dir(path.parent)
        finally:
            tmp.unlink(missing_ok=True)


def artifact(directory, assignment, name, body, *, reusable=False):
    receipts = directory / "runtime-evidence"
    receipts.mkdir(mode=0o700, exist_ok=True)
    path = receipts / f"{assignment}-{name}.json"
    data = encoded(body)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if not reusable or bounded(path, len(data)) != data:
            raise
    sync_dir(receipts)
    sync_dir(directory)
    return {"name": name, "path": str(path), "sha256": digest(data)}


def document(data):
    record = json.loads(data)
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 2:
        raise ValueError("Explicit v2 registry required")
    return record


def registry(goal):
    if goal.is_symlink():
        raise ValueError("Registry symlink forbidden")
    return goal.read_bytes()


def participant(project, participant_id):
    rows = [p for p in project["participants"] if p["id"] == participant_id]
    if len(rows) != 1:
        raise ValueError("Missing or ambiguous registered participant")
    return rows[0]["herdr"]


def task_record(project, task_id):
    rows = [t for t in project["deliverables"] if t["id"] == task_id]
    if len(rows) != 1:
        raise ValueError("Missing or ambiguous task")
    return rows[0]


def terminal(herdr, handle, *, allow_finished=False):
    address = handle["terminal_id"]
    if not re.fullmatch(r"t[0-9][0-9a-zA-Z]*", address):
        raise ValueError("Not a local registered terminal address")
    row = json.loads(herdr("get", "--json", address))
    states = {"live", "finished"} if allow_finished else {"live"}
    if (not isinstance(row, dict) or row.get("state") not in states or row.get("address") != address
            or type(row.get("created_ms")) is not int or row["created_ms"] != handle["created_ms"]):
        raise ValueError("Terminal identity changed or is unknown")
    if any(line.strip() == OWNER_HOLD for line in herdr("read", "--lines", "0", address).splitlines()):
        raise ValueError("Terminal owner hold")
    if row["state"] == "finished":
        return "finished"
    status = row.get("status")
    return status.get("state") if isinstance(status, dict) else None


def stopped_writer(herdr, attempt):
    if terminal(herdr, attempt["herdr"], allow_finished=True) not in {"idle", "done", "finished"}:
        raise ValueError("Exact old writer is still active or not demonstrably idle/done/finished")


def actor_context(herdr, project):
    # HC_TERMINAL_ID is supplied by the Herdr launch. This guards the
    # supported local path; it is not authentication against the same UID.
    address = os.environ.get("HC_TERMINAL_ID")
    rows = [p for p in project["participants"] if p["herdr"]["terminal_id"] == address]
    if len(rows) != 1:
        raise ValueError("Command requires unambiguous registered local terminal context")
    terminal(herdr, rows[0]["herdr"])
    return rows[0]


def coordinator(herdr, project):
    actor = actor_context(herdr, project)
    if actor["id"] != project["coordinator"]:
        raise ValueError("Command requires the registered coordinator")
    return actor["herdr"]


def graph(directory, herdr, goal, project):
    from herdr_cycle_gate import build_graph

    docs = [project]
    for peer in sorted((directory / "goals").glob("*.json")):
        if peer == goal:
            continue
        try:
            docs.append(document(registry(peer)))
        except (OSError, ValueError):
            # Missing/invalid dependency targets remain blocked by the model.
            continue
    registered = set()
    for doc in docs:
        participants = doc.get("participants")
        if not isinstance(participants, list):
            continue
        for item in participants:
            handle = item.get("herdr") if isinstance(item, dict) else None
            addr = handle.get("terminal_id") if isinstance(handle, dict) else None
            if isinstance(addr, str) and re.fullmatch(r"t[0-9][0-9a-zA-Z]*", addr):
                registered.add(addr)
    observed = {}
    live = {row["address"] for row in json.loads(herdr("ls", "--json"))["terminals"]
            if isinstance(row, dict) and isinstance(row.get("address"), str)}
    for addr in sorted(registered & live):
        try:
            actual = json.loads(herdr("get", "--json", addr))
        except subprocess.SubprocessError:
            continue
        if isinstance(actual, dict) and actual.get("state") == "live" and actual.get("address") == addr and type(actual.get("created_ms")) is int:
            observed[addr] = actual["created_ms"]
    return build_graph([{k: v for k, v in doc.items() if k not in RUNTIME_FIELDS} for doc in docs], observed=observed)


@contextmanager
def open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular file")
        yield stream


def bounded(path, size):
    with open_regular(path) as stream:
        data = stream.read(size + 1)
    if len(data) > size:
        raise ValueError("Input exceeds size limit")
    return data


def read_artifact(proof):
    raw = bounded(proof["path"], 65536)
    if not raw or digest(raw) != proof["sha256"]:
        raise ValueError("Missing or stale proof")
    item = json.loads(raw)
    if not isinstance(item, dict):
        raise ValueError("Structured proof must be an object")
    return item


def prior_settlement(task):
    if not task["attempts"]:
        return
    previous = task["attempts"][-1]
    dispatched = read_artifact(previous["checkpoints"][0]["evidence"])
    if previous["state"] != "settled" or dispatched.get("herdr") != previous["herdr"]:
        raise ValueError("Prior writer is not demonstrably settled")
    verify_proof(previous["checkpoints"][-1]["evidence"])
    if dispatched.get("assignment_id") != previous["id"] or dispatched.get("scope") != previous["scope"]:
        raise ValueError("Prior assignment identity does not match its admission")
    expected = {
        "assignment_id": previous["id"], "herdr": previous["herdr"], "scope": previous["scope"],
        "checkpoint": previous["checkpoints"][-1], "state": "settled",
    }
    proof = read_artifact(previous["settlement"])
    bound_observation(proof, expected)


def preflight(directory, goal, raw, herdr, task_id, message):
    project = document(raw)
    cos = coordinator(herdr, project)
    task = task_record(project, task_id)
    owner = participant(project, task["owner"])
    view = graph(directory, herdr, goal, project)
    rows = view.view(project_id=project["project_id"], task_id=task_id)
    if len(rows) != 1 or not rows[0]["ready"]:
        raise ValueError("Task is not graph-ready")
    prior_settlement(task)
    if terminal(herdr, owner) != "idle":
        raise ValueError("Assignment owner is not idle")
    if not message.strip() or len(message.encode()) > 8192 or "\x00" in message:
        raise ValueError("Nonempty assignment text (max 8192 bytes) required")
    from herdr_cycle_gate import attempt_id

    identity = {"project_id": project["project_id"], "task_id": task_id,
                "attempt": len(task["attempts"]) + 1, "owner": task["owner"],
                "herdr": owner, "scope": task["scope"]}
    assignment = attempt_id(identity["project_id"], task_id, identity["attempt"])
    intent = {**identity, "assignment_id": assignment, "message_sha256": digest(message.encode())}
    challenge = digest(encoded({"goal": str(goal), "registry": digest(raw), "intent": intent, "coordinator": cos}))
    return project, view, intent, challenge


def transport(herdr, intent, message):
    assignment = intent["assignment_id"]
    payload = f"Assignment-ID: {assignment}\nRegistered scope: {intent['scope']}\n{message}"
    reply = json.loads(herdr("send", "--json", "--if", "idle", intent["herdr"]["terminal_id"], payload))
    # Installed Core SendResult: turn is the required transport fact. A
    # started hint is not a checkpoint authenticated by the task owner.
    if (not isinstance(reply, dict) or type(reply.get("turn")) is not int or reply["turn"] < 0
            or reply.get("status") not in (None, "idle")
            or reply.get("started") is not None and type(reply["started"]) is not bool
            or reply.get("wait") is not None):
        raise ValueError("Unrecognized Herdr send result")
    return {"assignment_id": assignment, "turn": reply["turn"]}


def goal_path(directory, name):
    if Path(name).name != name or not name.endswith(".json"):
        raise ValueError("Supply an existing goals/*.json basename")
    return directory / "goals" / name


def bound_observation(proof, binding, extra=()):
    if set(proof) != set(binding) | {"observation", *extra} or any(proof[k] != value for k, value in binding.items()):
        raise ValueError("Proof must bind assignment, terminal generation, scope and last checkpoint")
    observation = proof["observation"]
    if not isinstance(observation, dict) or set(observation) != {"name", "path", "sha256"}:
        raise ValueError("An independent observation artifact is required")
    if not observation["name"] or not Path(observation["path"]).is_absolute():
        raise ValueError("An absolute named observation artifact is required")
    verify_proof(observation)


def record(directory, herdr, argv):
    parser = argparse.ArgumentParser(description="Record a registered v2 lifecycle event; never sends")
    parser.add_argument("goal")
    parser.add_argument("task_id")
    parser.add_argument("event", choices=("started", "implemented", "settle", "reassign", "accept", "integrate"))
    parser.add_argument("--proof-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        goal = goal_path(directory, args.goal)
        with lock(directory):
            raw = registry(goal)
            project = document(raw)
            actor = actor_context(herdr, project)["id"]
            task = task_record(project, args.task_id)
            view = graph(directory, herdr, goal, project)
            rows = view.view(project_id=project["project_id"], task_id=args.task_id)
            if len(rows) != 1 or rows[0]["held"] or "malformed" in rows[0]["blockers"]:
                raise ValueError("Task is malformed or held")
            body_bytes = bounded(args.proof_file, 65536)
            body = json.loads(body_bytes)
            if not isinstance(body, dict):
                raise ValueError("Expected structured proof")
            event = args.event
            payload = {}
            if event in {"started", "implemented", "settle"}:
                previous = task["attempts"][-1] if task["attempts"] else None
                if not previous or previous["state"] != "active":
                    raise ValueError("No active attempt")
                assignment = previous["id"]
                if assignment not in project.get("runtime_admissions", {}):
                    raise ValueError("Attempt was not admitted by the supported runtime")
                required_actor = project["coordinator"] if event == "settle" else previous["owner"]
                if actor != required_actor:
                    raise ValueError("Actor lacks authority for this attempt")
                if event == "settle":
                    stopped_writer(herdr, previous)
                binding = {"assignment_id": assignment, "herdr": previous["herdr"], "scope": previous["scope"],
                           "checkpoint": previous["checkpoints"][-1],
                           "state": "settled" if event == "settle" else event}
                bound_observation(body, binding, ("work", "artifacts") if event == "implemented" else ())
                # The immutable proof sits beside the admission. Graph validates
                # both its hash and the current exact attempt metadata.
                evidence = {**artifact(directory, assignment, f"{event}-{digest(encoded(body))}", body, reusable=True),
                            "attempt_id": assignment, "herdr": previous["herdr"], "scope": previous["scope"]}
                payload["evidence"] = evidence
                if event != "settle":
                    payload["stage"] = event
                    if event == "implemented":
                        payload.update(work=body["work"], artifacts=body["artifacts"])
                    event = "checkpoint"
            elif event == "reassign":
                if actor != project["coordinator"] or set(body) != {"owner"}:
                    raise ValueError("Reassignment requires coordinator and exact registered owner")
                prior_settlement(task)
                terminal(herdr, participant(project, body["owner"]))
                payload["owner"] = body["owner"]
            elif event == "accept":
                prior_settlement(task)
                payload["review"] = body
            elif body != {}:
                raise ValueError("Integrate uses an empty proof object and existing verified evidence")
            updated = view.transition(project["project_id"], args.task_id, event, actor, **payload)
            if actor_context(herdr, project)["id"] != actor:
                raise ValueError("Actor identity changed during recording")
            if args.event in {"started", "implemented", "settle"}:
                verify_proof(body["observation"])
            if args.event == "settle":
                stopped_writer(herdr, previous)
            if args.event == "reassign":
                terminal(herdr, participant(project, body["owner"]))
            for field in RUNTIME_FIELDS & project.keys():
                updated[field] = project[field]
            persist(goal, raw, updated)
            print(json.dumps({"state": "recorded", "event": args.event, "task_id": args.task_id}))
            return 0
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError) as error:
        print(json.dumps({"state": "refused", "reason": str(error)}))
        return 3


def observations(directory, herdr, argv):
    parser = argparse.ArgumentParser(description="Read CoS-facing structured recovery observations; never sends")
    parser.add_argument("goal")
    args = parser.parse_args(argv)
    try:
        with lock(directory):
            project = document(registry(goal_path(directory, args.goal)))
            coordinator(herdr, project)
            print(json.dumps(project.get("runtime_recovery", {}), sort_keys=True))
            return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"state": "refused", "reason": str(error)}))
        return 3


def command(directory, herdr, argv):
    if argv[:1] == ["record"]:
        return record(directory, herdr, argv[1:])
    if argv[:1] == ["observations"]:
        return observations(directory, herdr, argv[1:])
    parser = argparse.ArgumentParser(description="Registered coordinator v2 preflight or assignment")
    parser.add_argument("action", choices=("preflight", "assign"))
    parser.add_argument("goal", help="Existing registry filename in goals/")
    parser.add_argument("task_id")
    parser.add_argument("--message-file", type=Path, required=True)
    parser.add_argument("--challenge")
    args = parser.parse_args(argv)
    try:
        goal = goal_path(directory, args.goal)
        message = bounded(args.message_file, 8192).decode()
        with lock(directory):
            raw = registry(goal)
            p, view, intent, challenge = preflight(directory, goal, raw, herdr, args.task_id, message)
            if args.action == "preflight":
                print(json.dumps({"challenge": challenge, **intent}, sort_keys=True))
                return 0
            if args.challenge != challenge:
                raise ValueError("Preflight challenge missing or stale")
            assignment = intent["assignment_id"]
            evidence = {**artifact(directory, assignment, "admission", intent),
                        "attempt_id": assignment, "herdr": intent["herdr"], "scope": intent["scope"]}
            updated = view.transition(p["project_id"], args.task_id, "dispatch", p["coordinator"], evidence=evidence)
            for field in RUNTIME_FIELDS & p.keys():
                updated[field] = p[field]
            admissions = {**updated.get("runtime_admissions", {})}
            if assignment in admissions:
                raise ValueError("Assignment admission is already recorded")
            admissions[assignment] = {**intent, "evidence": evidence}
            updated["runtime_admissions"] = admissions
            try:
                persist(goal, raw, updated)
                # Revalidate mutable prerequisites, identity and screen holds
                # with a fresh graph before transport. Keep admission on refusal.
                if registry(goal) != encoded(updated):
                    raise ValueError("Goal changed after admission")
                _, _, _, live_challenge = preflight(directory, goal, raw, herdr, args.task_id, message)
                if live_challenge != challenge or registry(goal) != encoded(updated):
                    raise ValueError("Goal changed immediately before transport")
                returned = transport(herdr, intent, message)
                artifact(directory, assignment, "transport-returned", returned)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                print(json.dumps({"state": "reconciliation_required", "assignment_id": assignment}))
                return 4
            print(json.dumps({"state": "transport_returned", "assignment_id": assignment}))
            return 0
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError) as error:
        print(json.dumps({"state": "refused", "reason": str(error)}))
        return 3


def observe(directory, goal, herdr, stable, idle_seconds):
    """Record one CoS-facing observation per owner/checkpoint, never send."""
    with lock(directory):
        raw = registry(goal)
        project = document(raw)
        view = graph(directory, herdr, goal, project)
        recovery = project.setdefault("runtime_recovery", {})
        fresh = set()
        now = time.monotonic()
        changed = False
        for row in view.view(project_id=project["project_id"]):
            if set(row["blockers"]) - {"not_planned"} or row["status"] not in {"in_progress", "implemented"}:
                continue
            task = task_record(project, row["task_id"])
            attempts = task["attempts"]
            if not attempts or attempts[-1]["state"] != "active":
                continue
            active = attempts[-1]
            checkpoint = active["checkpoints"][-1]
            if active["owner"] != task["owner"] or active["herdr"] != participant(project, task["owner"]):
                continue
            try:
                intent = read_artifact(active["checkpoints"][0]["evidence"])
                if any(intent.get(k) != v for k, v in {
                    "project_id": project["project_id"], "task_id": row["task_id"], "attempt": active["number"],
                    "owner": active["owner"], "herdr": active["herdr"], "scope": active["scope"],
                    "assignment_id": active["id"],
                }.items()):
                    continue
                verify_proof(checkpoint["evidence"])
                terminal(herdr, participant(project, project["coordinator"]))
                if terminal(herdr, active["herdr"]) not in {"idle", "done"}:
                    continue
                assignment = intent["assignment_id"]
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                continue
            observation = {"assignment_id": assignment, "task_id": row["task_id"],
                           "owner": active["owner"], "herdr": active["herdr"], "checkpoint": checkpoint}
            key = digest(encoded(observation))
            if key in recovery:
                continue
            fresh.add(key)
            if now - stable.setdefault(key, now) < idle_seconds:
                continue
            recovery[key] = {**observation, "state": "coordinator_reconciliation_required"}
            changed = True
        for key in set(stable) - fresh:
            stable.pop(key)
        if changed:
            persist(goal, raw, project)
        return changed


def verify_proof(proof):
    result = hashlib.sha256()
    size = 0
    with open_regular(proof["path"]) as stream:
        for block in iter(lambda: stream.read(131072), b""):
            size += len(block)
            result.update(block)
    if not size or result.hexdigest() != proof["sha256"]:
        raise ValueError("Missing or stale checkpoint")
