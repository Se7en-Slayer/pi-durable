"""Opt-in, CoS-routed idle reflection on the existing healer tick and ledger."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

from herdr_cycle_gate import attempt_id
from herdr_graph_runtime import (
    OWNER_HOLD, bounded, digest, encoded, goal_path, lock, persist, prior_settlement,
    read_artifact, registry, verify_proof,
)

IDLE_SECONDS = 600
MAX_GAP_SECONDS = 45  # Normal tick is 30s; a missed tick is not stable observation.
FIELDS = ("worked", "failed", "root_cause_or_limits", "carry_forward")
BLOCKED = ("held", "paused", "stood_down")
ERRORS = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def available(record, *, explicit=False):
    return record.get("state") not in BLOCKED and all(
        record.get(flag, None if explicit else False) is False for flag in BLOCKED)


def schema(project):
    version = project.get("schema_version", 1)
    require(type(version) is int and version in (1, 2), "Unsupported goal version")
    project_id = project.get("project_id" if version == 2 else "reflection_project_id")
    require(text(project_id), "Missing canonical reflection project identity")
    return version, project_id


def roster(project):
    version, _ = schema(project)
    if version == 1:
        root = project["supervisor"]["address"]
        rows = [{"id": row["address"], "parent": None if row["address"] == root else root,
                 "herdr": {"terminal_id": row["address"], "created_ms": row["created_ms"]}}
                for row in [project["supervisor"], *project.get("workers", [])]]
    else:
        root, rows = project["coordinator"], project["participants"]
    require(isinstance(rows, list) and all(isinstance(row, dict) and text(row.get("id")) for row in rows),
            "Invalid participant roster")
    people = {row["id"]: row for row in rows}
    require(len(people) == len(rows) and root in people and people[root].get("parent") is None,
            "Missing or ambiguous coordinator")
    return root, people


def registered(people, root, participant_id):
    person = people[participant_id]
    handle = person["herdr"]
    require(isinstance(handle, dict) and set(handle) == {"terminal_id", "created_ms"} and
            isinstance(handle["terminal_id"], str) and re.fullmatch(r"t[0-9][0-9a-zA-Z]*", handle["terminal_id"]) and
            type(handle["created_ms"]) is int and handle["created_ms"] > 0 and
            sum(row.get("herdr") == handle for row in people.values()) == 1, "Unregistered or ambiguous terminal")
    seen = set()
    cursor = participant_id
    while cursor != root:
        require(cursor in people and cursor not in seen and available(people[cursor]), "Invalid or held participant nesting")
        seen.add(cursor)
        cursor = people[cursor]["parent"]
    require(available(people[root]), "Coordinator is held")
    return handle


def proof_shape(proof):
    require(isinstance(proof, dict) and set(proof) == {"name", "path", "sha256"} and text(proof["name"]) and
            text(proof["path"]) and Path(proof["path"]).is_absolute() and
            isinstance(proof["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", proof["sha256"]), "Invalid evidence identity")


def binding_identity(project, binding, root, people):
    version, project_id = schema(project)
    require(available(project) and available(binding, explicit=True), "Reflection is held, paused or stood down")
    participant_id = binding["participant_id"]
    handle = registered(people, root, participant_id)
    assignment, turn = binding["assignment_id"], binding["turn"]
    require(isinstance(assignment, str) and re.fullmatch(r"[a-f0-9]{32}(?:[a-f0-9]{32})?", assignment) and
            type(turn) is int and turn >= 0 and binding["herdr"] == handle, "Binding lacks exact assignment, turn or handle")
    identity = {"project_id": project_id, "participant_id": participant_id,
                "assignment_id": assignment, "herdr": handle, "turn": turn}
    evidence = binding["evidence"]
    proof_shape(evidence)
    require(read_artifact(evidence) == {**identity, "state": "settled"}, "Evidence does not bind the settled turn")
    final = False
    if version == 2:
        linked = [task for task in project["deliverables"]
                  if any(attempt.get("id") == assignment for attempt in task["attempts"])]
        if linked or "task_id" in binding:
            require(len(linked) == 1, "Missing or ambiguous graph assignment")
            task = linked[0]
            latest = task["attempts"][-1]
            require(binding.get("task_id") == task["id"] and available(task) and task["level"] in (1, 2) and
                    task["status"] in {"implemented", "verified", "integrated"} and task["owner"] == participant_id and
                    latest["id"] == assignment and latest["owner"] == participant_id and latest["herdr"] == handle and
                    latest["state"] == "settled", "Graph assignment is not the latest settled work")
            admission = project.get("runtime_admissions", {}).get(assignment)
            require(assignment == attempt_id(project_id, task["id"], latest["number"]) and
                    isinstance(admission, dict) and all(admission.get(key) == value for key, value in {
                        "project_id": project_id, "task_id": task["id"], "owner": participant_id, "herdr": handle,
                        "scope": latest["scope"], "assignment_id": assignment,
                        "evidence": latest["checkpoints"][0]["evidence"],
                    }.items()), "Graph assignment lacks an exact supported admission")
            prior_settlement(task)
            final = task["status"] in {"verified", "integrated"}
    else:
        require("task_id" not in binding, "Legacy opt-in uses explicit recorded identity, not graph attempts")
    return identity, final


def reflection_id(identity):
    return digest(encoded({key: identity[key] for key in ("assignment_id", "herdr", "turn")}))


def scope(identity):
    return {"kind": "self-reflection-only", "participant_id": identity["participant_id"],
            "assignment_id": identity["assignment_id"], "turn": identity["turn"]}


def terminal_sample(herdr, handle, *, turn=None):
    address = handle["terminal_id"]
    raw = json.loads(herdr("get", "--json", address))
    status = raw["status"]["state"]
    require(raw["address"] == address and type(raw["created_ms"]) is int and
            raw["created_ms"] == handle["created_ms"] and raw["state"] == "live" and raw["reported"] is True and
            type(raw["turn"]) is int and raw["turn"] >= 0 and type(raw["status_ms"]) is int,
            "Current terminal generation or turn is unavailable")
    require(turn is None or (raw["turn"] == turn and status in {"idle", "done"}), "Turn not settled or changed")
    require(not any(line.strip() == OWNER_HOLD for line in herdr("read", "--lines", "0", address).splitlines()),
            "Terminal owner hold")
    return (raw["turn"], status, raw["status_ms"])


def bindings(project):
    rows = project.get("reflection_bindings", [])
    require(isinstance(rows, list) and all(isinstance(row, dict) and text(row.get("participant_id")) for row in rows),
            "Invalid reflection binding list")
    ids = [row["participant_id"] for row in rows]
    assignments = [row.get("assignment_id") for row in rows]
    require(len(set(ids)) == len(ids) and all(isinstance(item, str) for item in assignments) and
            len(set(assignments)) == len(assignments), "Duplicate participant or canonical assignment binding")
    return rows


def peers(directory, goal):
    for path in sorted((directory / "goals").glob("*.json")):
        if path == goal:
            continue
        try:
            record = json.loads(registry(path))
            if isinstance(record, dict):
                yield record
        except ERRORS:
            continue


def records(value):
    return (item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()


def attempts(project):
    if project.get("schema_version") == 2:
        for task in records(project.get("deliverables")):
            yield from records(task.get("attempts"))


def busy(project, identity, final):
    rows = project.get("runtime_recovery", {})
    if not final and isinstance(rows, dict) and any(isinstance(row, dict) and
            row.get("assignment_id") == identity["assignment_id"] and
            row.get("state") == "coordinator_reconciliation_required" for row in rows.values()):
        return True
    return any(attempt.get("state") == "active" and
               (attempt.get("id") == identity["assignment_id"] or attempt.get("herdr") == identity["herdr"])
               for attempt in attempts(project))


def overlap(project, identity):
    return (any(row.get("assignment_id") == identity["assignment_id"] or
                (row.get("herdr") == identity["herdr"] and row.get("turn") == identity["turn"])
                for row in records(project.get("reflection_bindings"))) or
            any(attempt.get("id") == identity["assignment_id"] for attempt in attempts(project)))


def observe(directory, goal, herdr, stable, legacy_ready, *, clock=time.monotonic):
    """One existing watchdog tick. No dispatch/recovery calls."""
    fresh = set()
    try:
        with lock(directory):
            raw = registry(goal)
            project = json.loads(raw)
            require(isinstance(project, dict), "Expected goal object")
            if not project.get("reflection_bindings") or not available(project):
                return 0
            version, _ = schema(project)
            if version == 1 and not legacy_ready(project):
                return 0
            root, people = roster(project)
            queue = project.setdefault("runtime_reflections", {})
            require(isinstance(queue, dict), "Invalid durable reflection queue")
            others = list(peers(directory, goal))
            count = 0
            for binding in bindings(project):
                key = None
                try:
                    identity, final = binding_identity(project, binding, root, people)
                    key = reflection_id(identity)
                    if key in queue or any(isinstance(doc.get("runtime_reflections"), dict) and
                                           key in doc["runtime_reflections"] for doc in others):
                        continue
                    if busy(project, identity, final) or any(overlap(doc, identity) or busy(doc, identity, False)
                                                            for doc in others):
                        continue
                    sample = (terminal_sample(herdr, identity["herdr"], turn=identity["turn"]),
                              digest(encoded(binding)))
                    now = clock()
                    previous, start, last = stable.get(key, (None, now, now))
                    if previous != sample or now < last or now - last > MAX_GAP_SECONDS:
                        start = now
                    stable[key] = (sample, start, now)
                    fresh.add(key)
                    if now - start < IDLE_SECONDS:
                        continue
                    if terminal_sample(herdr, identity["herdr"], turn=identity["turn"]) != sample[0]:
                        fresh.discard(key)
                        continue
                    queue[key] = {**identity, "reflection_id": key, "scope": scope(identity),
                                  "state": "coordinator_review_required", "stable_seconds": int(now-start),
                                  "subject_evidence": binding["evidence"], "requested": list(FIELDS)}
                    count += 1
                except ERRORS:
                    if key is not None:
                        fresh.discard(key)
                    continue
            if count:
                persist(goal, raw, project)
                raw = encoded(project)
            notify_one(directory, goal, raw, project, root, people, others, herdr, legacy_ready)
            return count
    except ERRORS:
        fresh.clear()
        raise
    finally:
        for key in set(stable) - fresh:
            stable.pop(key, None)


def idle_target(herdr, handle, *, turn=None):
    require(terminal_sample(herdr, handle, turn=turn)[1] == "idle", "Recipient is not idle")


def delivery_id(reflection, target, kind):
    return digest(encoded({"reflection_id": reflection, "recipient": target, "kind": kind}))


def revalidate_delivery(directory, goal, intent, row, field, target, herdr, legacy_ready):
    require(registry(goal) == intent, "Registry changed after intent")
    project = json.loads(intent)
    root, people = roster(project)
    coordinator = registered(people, root, root)
    rows = {binding["participant_id"]: binding for binding in bindings(project)}
    require(root not in rows or available(rows[root], explicit=True), "Coordinator reflection binding is held")
    require(schema(project)[0] != 1 or legacy_ready(project), "Legacy recovery is unresolved")
    binding = rows[row["participant_id"]]
    identity, final = binding_identity(project, binding, root, people)
    key = reflection_id(identity)
    admission(row, key, identity, binding)
    require(row["state"] == "coordinator_review_required" and not busy(project, identity, final),
            "Reflection no longer eligible")
    others = list(peers(directory, goal))
    require(not any(overlap(doc, identity) or busy(doc, identity, False) or
                    (isinstance(doc.get("runtime_reflections"), dict) and key in doc["runtime_reflections"])
                    for doc in others), "Conflicting reflection ownership or recovery")
    subject = identity["herdr"]
    if field == "notification":
        require(target == coordinator, "Notification recipient changed")
        coordinator_work = {"assignment_id": "", "herdr": coordinator}
        require(not any(busy(doc, coordinator_work, False) for doc in [project, *others]), "Coordinator has active work")
        terminal_sample(herdr, subject, turn=identity["turn"])
        idle_target(herdr, coordinator, turn=identity["turn"] if subject == coordinator else None)
    else:
        require(field == "handoff" and target == subject and subject != coordinator, "Handoff recipient changed")
        actor(herdr, root, people, root)
        idle_target(herdr, subject, turn=identity["turn"])
    # Core's idle guard does not atomically check generations, evidence or holds.
    require(binding_identity(project, binding, root, people) == (identity, final) and registry(goal) == intent,
            "Reflection evidence or registry changed before send")


def dispatch_once(directory, goal, raw, project, row, field, target, message, herdr, legacy_ready, *, routed_by=None):
    """Persist intent before transport; an existing intent is never replayed."""
    require(field not in row, "Delivery intent already exists")
    identity = {"reflection_id": row["reflection_id"], "recipient": target,
                "scope": row["scope"], "message_sha256": digest(message.encode())}
    delivery = {**identity, "id": delivery_id(row["reflection_id"], target, field), "state": "intent"}
    row[field] = delivery
    persist(goal, raw, project)
    intent = encoded(project)
    try:
        revalidate_delivery(directory, goal, intent, row, field, target, herdr, legacy_ready)
    except ERRORS:
        delivery["state"] = "blocked_before_send"
        persist(goal, intent, project)
        return False
    try:
        reply = json.loads(herdr("send", "--json", "--if", "idle", target["terminal_id"], message))
        require(isinstance(reply, dict) and type(reply.get("turn")) is int and reply["turn"] >= 0 and
                reply.get("status") in (None, "idle") and reply.get("wait") is None and
                (reply.get("started") is None or type(reply["started"]) is bool), "Unrecognized send result")
    except ERRORS:
        delivery["state"] = "uncertain"
    else:
        delivery.update(state="delivered", turn=reply["turn"])
        if routed_by is not None:
            row.update(state="routed", routed_by=routed_by)
    persist(goal, intent, project)
    return delivery["state"] == "delivered"


def notify_one(directory, goal, raw, project, root, people, others, herdr, legacy_ready):
    target = registered(people, root, root)
    if busy(project, {"assignment_id": "", "herdr": target}, False) or any(
            busy(doc, {"assignment_id": "", "herdr": target}, False) for doc in others):
        return
    eligible = {binding["participant_id"]: binding for binding in bindings(project)}
    if root in eligible and not available(eligible[root], explicit=True):
        return
    for key, row in project["runtime_reflections"].items():
        if not isinstance(row, dict) or row.get("state") != "coordinator_review_required" or (
                "notification" in row or "handoff" in row):
            continue
        try:
            binding = eligible[row["participant_id"]]
            identity, final = binding_identity(project, binding, root, people)
            require(reflection_id(identity) == key, "Original assignment or turn differs")
            admission(row, key, identity, binding)
            if busy(project, identity, final) or any(overlap(doc, identity) or busy(doc, identity, False) for doc in others):
                continue
            terminal_sample(herdr, identity["herdr"], turn=identity["turn"])
            idle_target(herdr, target)
        except ERRORS:
            continue
        dispatch_once(directory, goal, raw, project, row, "notification", target,
                      f"Idle reflection due: {key} (goal {goal.name}). Review and route through the registered CoS path only. No new implementation authority.",
                      herdr, legacy_ready)
        return  # At most one notification to the coordinator per goal tick.


def actor(herdr, root, people, participant_id, *, min_turn=0):
    handle = registered(people, root, participant_id)
    require(os.environ.get("HC_TERMINAL_ID") == handle["terminal_id"], "Command requires the exact registered actor")
    turn, status, _ = terminal_sample(herdr, handle)
    require(turn >= min_turn and status in {"idle", "done", "working"}, "Current actor or routed turn is unavailable")
    return participant_id


def admission(row, reflection, identity, binding):
    require(row == {**identity, "reflection_id": reflection, "scope": scope(identity),
                    "state": row.get("state"), "stable_seconds": row.get("stable_seconds"),
                    "subject_evidence": binding["evidence"], "requested": list(FIELDS),
                    **{key: row[key] for key in ("notification", "handoff", "routed_by") if key in row}} and
            type(row["stable_seconds"]) is int and row["stable_seconds"] >= IDLE_SECONDS,
            "Missing or mismatched durable reflection admission")


def command(directory, herdr, argv, legacy_ready):
    parser = argparse.ArgumentParser(description="Inspect, explicitly route or record durable idle reflection")
    parser.add_argument("action", choices=("reflections", "reflection-route", "reflection-record"))
    parser.add_argument("goal")
    parser.add_argument("reflection", nargs="?")
    parser.add_argument("--proof-file", type=Path)
    args = parser.parse_args(argv)
    try:
        goal = goal_path(directory, args.goal)
        with lock(directory):
            raw = registry(goal)
            project = json.loads(raw)
            require(isinstance(project, dict), "Expected goal object")
            root, people = roster(project)
            queue = project.get("runtime_reflections", {})
            require(isinstance(queue, dict), "Invalid reflection queue")
            if args.action == "reflections":
                require(args.reflection is None and args.proof_file is None, "List takes only a goal")
                actor(herdr, root, people, root)
                print(json.dumps(queue, sort_keys=True))
                return 0
            require(isinstance(args.reflection, str) and re.fullmatch(r"[a-f0-9]{64}", args.reflection),
                    "An exact durable reflection ID is required")
            row = queue[args.reflection]
            require(isinstance(row, dict), "Invalid reflection admission")
            rows = [binding for binding in bindings(project) if binding["participant_id"] == row["participant_id"]]
            require(len(rows) == 1, "Original binding no longer registered")
            binding = rows[0]
            identity, final = binding_identity(project, binding, root, people)
            require(reflection_id(identity) == args.reflection, "Original assignment or turn differs")
            require(schema(project)[0] != 1 or legacy_ready(project), "Legacy recovery is unresolved")
            admission(row, args.reflection, identity, binding)
            others = list(peers(directory, goal))
            require(not any(overlap(doc, identity) or (isinstance(doc.get("runtime_reflections"), dict) and
                        args.reflection in doc["runtime_reflections"]) for doc in others), "Conflicting reflection ownership")
            if args.action == "reflection-route":
                require(args.proof_file is None and row["state"] == "coordinator_review_required" and
                        "handoff" not in row and "routed_by" not in row and not busy(project, identity, final) and
                        not any(busy(doc, identity, False) for doc in others), "Reflection cannot be routed")
                require(all(available(item, explicit=True) for item in bindings(project) if item["participant_id"] == root),
                        "Coordinator reflection binding is held")
                routed_by = actor(herdr, root, people, root)
                target = identity["herdr"]
                if identity["participant_id"] == root:
                    row.update(state="routed", routed_by=routed_by, handoff={"state": "self_routed",
                        "id": delivery_id(args.reflection, target, "self"),
                        "reflection_id": args.reflection, "recipient": target, "scope": row["scope"]})
                else:
                    idle_target(herdr, target, turn=identity["turn"])
                    handoff = delivery_id(args.reflection, target, "handoff")
                    message = (f"Reflection-ID: {args.reflection}\nHandoff-ID: {handoff}\n"
                               f"Original assignment: {identity['assignment_id']}; turn: {identity['turn']}.\n"
                               "Scope: self-reflection only. Record what worked, failed, the root cause or limits, "
                               "and what to carry forward, with exact hashed evidence. Do not start other work.")
                    delivered = dispatch_once(directory, goal, raw, project, row, "handoff", target, message,
                                              herdr, legacy_ready, routed_by=routed_by)
                    print(json.dumps({"state": row["state"] if delivered else "reconciliation_required",
                                      "reflection_id": args.reflection}))
                    return 0 if delivered else 4
            else:
                handoff = row.get("handoff")
                require(args.proof_file is not None and row["state"] == "routed" and row.get("routed_by") == root and
                        isinstance(handoff, dict) and handoff.get("state") in {"delivered", "self_routed"} and
                        handoff.get("reflection_id") == args.reflection and handoff.get("recipient") == identity["herdr"] and
                        handoff.get("scope") == row["scope"], "CoS routed handoff and exact proof are required")
                self_route = handoff["state"] == "self_routed"
                require(self_route == (identity["participant_id"] == root) and handoff.get("id") ==
                        delivery_id(args.reflection, identity["herdr"], "self" if self_route else "handoff") and
                        (self_route or (type(handoff.get("turn")) is int and handoff["turn"] >= 0)),
                        "Stored handoff identity or delivery turn is invalid")
                body = json.loads(bounded(args.proof_file, 65536))
                expected = {**identity, "reflection_id": args.reflection, "handoff_id": handoff["id"]}
                require(isinstance(body, dict) and set(body) == {*expected, *FIELDS, "evidence"} and
                        all(body[key] == value for key, value in expected.items()) and
                        all(text(body[field]) for field in FIELDS) and isinstance(body["evidence"], list) and body["evidence"],
                        "Reflection proof does not bind the original assignment and include all fields/evidence")
                for evidence in body["evidence"]:
                    proof_shape(evidence)
                    verify_proof(evidence)
                row.update(state="recorded", recorded_by=actor(herdr, root, people, identity["participant_id"],
                                                               min_turn=0 if self_route else handoff["turn"]), result=body)
            persist(goal, raw, project)
            print(json.dumps({"state": row["state"], "reflection_id": args.reflection}))
            return 0
    except ERRORS:
        print(json.dumps({"state": "refused"}))
        return 3
