"""Authoritative same-partition CAS. Only the test backend uses a local lock.

Every mutation replaces the pool ETag AND all changed entities in one transaction.
Read root before listing entities: a concurrent mutation invalidates that root ETag.
No external action is performed inside a retried state mutation.
"""

import copy
import json
import time
import uuid


class Conflict(Exception):
    pass


class TableBackend:
    def __init__(self, endpoint, table, partition, credential):
        from azure.data.tables import TableClient
        from azure.core.exceptions import ResourceExistsError
        self.client = TableClient(endpoint, table, credential=credential,
                                  retry_total=0, connection_timeout=10, read_timeout=20)
        self.partition = partition
        try:
            self.client.create_entity({"PartitionKey": partition, "RowKey": "pool",
                                       "payload": "{}", "revision": 0})
        except ResourceExistsError:
            pass

    def snapshot(self):
        root = self.client.get_entity(self.partition, "pool")
        rows = {}
        for entity in self.client.query_entities(
                "PartitionKey eq @p", parameters={"p": self.partition}):
            if entity["RowKey"] != "pool":
                rows[entity["RowKey"]] = (json.loads(entity["payload"]),
                                          entity.metadata["etag"])
        return root.metadata["etag"], root["revision"], rows

    def commit(self, etag, revision, before, after):
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError
        operations = [("update", {"PartitionKey": self.partition, "RowKey": "pool",
                                  "payload": "{}", "revision": revision + 1},
                       {"mode": "replace", "etag": etag,
                        "match_condition": MatchConditions.IfNotModified})]
        for key in before.keys() | after.keys():
            if key in before and key in after and before[key][0] == after[key]:
                continue
            entity = {"PartitionKey": self.partition, "RowKey": key}
            if key not in after:
                operations.append(("delete", entity, {"etag": before[key][1],
                                   "match_condition": MatchConditions.IfNotModified}))
            else:
                entity["payload"] = json.dumps(after[key], separators=(",", ":"))
                if len(entity["payload"].encode("utf-16-le")) > 60000:
                    raise ValueError("Control entity too large")
                if key in before:
                    operations.append(("update", entity, {"mode": "replace",
                                       "etag": before[key][1],
                                       "match_condition": MatchConditions.IfNotModified}))
                else:
                    operations.append(("create", entity))
        if len(operations) > 100:
            raise ValueError("Transaction bound exceeded")
        try:
            self.client.submit_transaction(operations)
        except HttpResponseError as exc:
            if exc.status_code in (409, 412):
                raise Conflict() from exc
            raise


class MemoryBackend:
    """Explicit test-only ETag/atomic-transaction emulator, never a fallback."""
    def __init__(self):
        import threading
        self.lock, self.revision, self.rows = threading.Lock(), 0, {}

    def snapshot(self):
        with self.lock:
            return self.revision, self.revision, {
                k: (copy.deepcopy(v), self.revision) for k, v in self.rows.items()}

    def commit(self, etag, revision, before, after):
        with self.lock:
            if etag != self.revision:
                raise Conflict()
            self.rows = copy.deepcopy(after)
            self.revision += 1


class PoolStore:
    def __init__(self, backend, limits, clock=time.time):
        self.backend, self.limits, self.clock = backend, limits, clock

    def change(self, action):
        for _ in range(30):
            etag, revision, before = self.backend.snapshot()
            rows = {k: copy.deepcopy(v[0]) for k, v in before.items()}
            result = action(rows, self.clock())
            workers = [v for k, v in rows.items() if k.startswith("w:")]
            if len(workers) > self.limits.max_total and (
                    not rows.get("control", {}).get("admission_paused")
                    or any(k.startswith("w:") and k not in before
                           and v["state"] != "draining" for k, v in rows.items())):
                raise RuntimeError("Capacity invariant violated")
            try:
                self.backend.commit(etag, revision, before, rows)
                return result
            except Conflict:
                continue
        raise RuntimeError("State contention; retry acquisition, never replay execution")

    def view(self):
        # Even reads use CAS to obtain an authoritative consistent snapshot.
        return self.change(lambda rows, now: copy.deepcopy(rows))

    @staticmethod
    def public(session):
        return {"session_id": session["id"], "status": session["state"],
                "generation": session["generation"]}

    def acquire(self, owner):
        sid = uuid.uuid4().hex
        def action(rows, now):
            if rows.get("control", {}).get("admission_paused"):
                raise PermissionError("Pool admission paused for administrative recovery")
            queued = sum(v["state"] == "queued" for k, v in rows.items() if k.startswith("s:"))
            if queued >= self.limits.queue_size:
                raise ValueError("Session queue full")
            s = {"id": sid, "owner": owner, "state": "queued", "generation": 1,
                 "created": now, "queue_until": now + self.limits.queue_wait}
            rows["s:" + sid] = s
            return self.public(s)
        return self.change(action)

    def owned(self, rows, sid, owner):
        s = rows.get("s:" + sid)
        if s is None or s["owner"] != owner:
            raise PermissionError("Unknown session")
        return s

    def status(self, sid, owner):
        self.sweep()
        return self.change(lambda rows, now: self.public(self.owned(rows, sid, owner)))

    def listing(self, owner):
        self.sweep()
        return self.change(lambda rows, now: [
            self.public(v) for k, v in rows.items()
            if k.startswith("s:") and v["owner"] == owner])

    @staticmethod
    def retire(rows, s, now, state="closed"):
        s["state"], s["generation"] = state, s["generation"] + 1
        s["terminal_at"] = now
        if "worker" in s:
            w = rows["w:" + s["worker"]]
            w["state"] = "draining"
            w.setdefault("delete_operation", uuid.uuid4().hex)
            w["delete_after"] = max(now, s.get("busy_until", now) if s.get("busy") else now)
            w.pop("claim", None)
            w.pop("claim_until", None)

    def release(self, sid, owner):
        def action(rows, now):
            s = self.owned(rows, sid, owner)
            if s["state"] not in ("closed", "expired", "failed"):
                self.retire(rows, s, now)
            return self.public(s)
        return self.change(action)

    def sweep(self):
        def action(rows, now):
            removed = 0
            sessions_changed = 0
            for key, s in list(rows.items()):
                if not key.startswith("s:"):
                    continue
                if sessions_changed >= 20:
                    continue
                if s["state"] == "queued" and now >= s["queue_until"]:
                    self.retire(rows, s, now, "expired")
                    sessions_changed += 1
                elif s["state"] in ("active", "activating"):
                    activity_until = max(s["idle_until"], s.get("busy_until", 0) if s.get("busy") else 0)
                    if now >= s["absolute_until"] or now >= activity_until:
                        self.retire(rows, s, now, "expired")
                        sessions_changed += 1
                    elif s.get("busy") and now >= s["busy_until"]:
                        self.retire(rows, s, now, "failed")
                        sessions_changed += 1
                elif (s["state"] in ("closed", "expired", "failed")
                      and now >= s.get("terminal_at", s["created"]) + self.limits.terminal_retention
                      and ("worker" not in s or "w:" + s["worker"] not in rows)):
                    # Bound each sweep transaction, including worker/session expiry changes.
                    if removed < 20:
                        del rows[key]
                        removed += 1
                        sessions_changed += 1
            for key, w in list(rows.items()):
                if (key.startswith("w:") and w["state"] in ("reserved", "provisioning")
                        and now >= w["created"] + self.limits.provisioning_age):
                    # Create may still be accepted late. Do not free capacity on absence.
                    w["state"] = "quarantined"
                    w["quarantine_reason"] = "provisioning-age"
                    w["quarantined_at"] = now
                    w.pop("claim", None)
                    w.pop("claim_until", None)
            if rows.get("control", {}).get("admission_paused"):
                count = sum(k.startswith("w:") for k in rows)
                rows["control"]["worker_count"] = count
        self.change(action)

    def begin(self, sid, owner):
        self.sweep()
        op = uuid.uuid4().hex
        def action(rows, now):
            s = self.owned(rows, sid, owner)
            if s["state"] != "active" or s.get("busy"):
                raise PermissionError("Session not active or operation already in flight")
            if now >= s["absolute_until"] or now >= s["idle_until"]:
                raise PermissionError("Session lease expired")
            w = rows["w:" + s["worker"]]
            if w["state"] != "active" or w["session"] != sid or w["generation"] != s["generation"]:
                raise PermissionError("Invalid worker lease")
            s["busy"], s["busy_until"] = op, min(now + self.limits.operation, s["absolute_until"])
            s["idle_until"] = now + self.limits.idle
            return copy.deepcopy(w), op, s["generation"], s["busy_until"] - now
        return self.change(action)

    def finish(self, sid, owner, op, generation, uncertain=False):
        def action(rows, now):
            s = self.owned(rows, sid, owner)
            if s.get("busy") != op:
                raise PermissionError("Stale operation")
            overdue = now >= s["busy_until"]
            s.pop("busy")
            if uncertain or overdue or now >= s["absolute_until"]:
                if s["state"] == "active":
                    self.retire(rows, s, now, "failed")
            valid = s["state"] == "active" and s["generation"] == generation
            s["idle_until"] = now + self.limits.idle
            return valid and not uncertain
        return self.change(action)

    def reserve(self):
        wid = uuid.uuid4().hex
        def action(rows, now):
            if rows.get("control", {}).get("admission_paused"):
                return None
            workers = [v for k, v in rows.items() if k.startswith("w:")]
            available = sum(v["state"] in ("reserved", "provisioning", "ready") for v in workers)
            if len(workers) >= self.limits.max_total or available >= self.limits.desired_ready:
                return None
            w = {"id": wid, "lifetime": uuid.uuid4().hex, "state": "reserved",
                 "created": now, "generation": 0, "create_operation": uuid.uuid4().hex}
            rows["w:" + wid] = w
            return copy.deepcopy(w)
        return self.change(action)

    def assign(self):
        def action(rows, now):
            if rows.get("control", {}).get("admission_paused"):
                return
            sessions = sorted((v for k, v in rows.items()
                               if k.startswith("s:") and v["state"] == "queued"
                               and now < v["queue_until"]), key=lambda s: s["created"])
            workers = [v for k, v in rows.items() if k.startswith("w:") and v["state"] == "ready"]
            for s, w in zip(sessions, workers):
                s.update(state="activating", worker=w["id"],
                         idle_until=now + self.limits.idle,
                         absolute_until=now + self.limits.absolute)
                w.update(state="activating", session=s["id"], generation=s["generation"])
        self.change(action)

    def claim(self, wid):
        op = uuid.uuid4().hex
        def action(rows, now):
            w = rows.get("w:" + wid)
            if not w or w["state"] in ("ready", "active", "quarantined"):
                return None
            if now < w.get("claim_until", 0) or now < w.get("delete_after", 0):
                return None
            # Activation may have reached the worker before a controller crash.
            if w["state"] == "activating" and w.get("claim"):
                self.retire(rows, rows["s:" + w["session"]], now, "failed")
                w = rows["w:" + wid]
            w.update(claim=op, claim_until=now + self.limits.claim)
            if w["state"] == "reserved":
                w["state"] = "provisioning"
            if w["state"] == "draining":
                w["state"] = "deleting"
                w.setdefault("delete_operation", uuid.uuid4().hex)
            return copy.deepcopy(w)
        return self.change(action)

    def complete(self, wid, op, state, routing=None):
        def action(rows, now):
            w = rows.get("w:" + wid)
            if not w or w.get("claim") != op or now >= w["claim_until"]:
                return False
            if state == "deleted":
                del rows["w:" + wid]
                return True
            if state == "active":
                s = rows["s:" + w["session"]]
                if s["state"] != "activating" or s["generation"] != w["generation"]:
                    return False
                if now >= s["absolute_until"] or now >= s["idle_until"]:
                    self.retire(rows, s, now, "expired")
                    return False
                s["state"] = "active"
            if routing:
                w["routing"] = routing
            w["state"] = state
            w.pop("claim", None)
            w.pop("claim_until", None)
            return True
        return self.change(action)

    def fail_worker(self, wid, op=None):
        def action(rows, now):
            w = rows.get("w:" + wid)
            if not w or (op is not None and w.get("claim") != op):
                return
            if "session" in w:
                s = rows["s:" + w["session"]]
                if s["state"] in ("active", "activating"):
                    self.retire(rows, s, now, "failed")
            w["state"] = "draining"
            w.setdefault("delete_operation", uuid.uuid4().hex)
            w["delete_after"] = max(now, w.get("delete_after", now))
            w.pop("claim", None)
            w.pop("claim_until", None)
        self.change(action)

    def reconcile_orphan(self, wid, lifetime):
        """Record all strictly-owned orphans, even over-cap, without admitting work."""
        def action(rows, now):
            if "w:" + wid in rows:
                if rows["w:" + wid]["lifetime"] != lifetime:
                    raise PermissionError("Resource/state lifetime mismatch; manual investigation required")
                return
            if sum(k.startswith("w:") for k in rows) >= self.limits.max_total:
                rows["control"] = {"admission_paused": True, "reason": "over-cap-owned-orphans"}
            rows["w:" + wid] = {
                "id": wid, "lifetime": lifetime, "state": "draining",
                "generation": 0, "created": now, "delete_after": now,
                "delete_operation": uuid.uuid4().hex}
        self.change(action)

    def recover(self, wid, action, expected_lifetime):
        """Admin CAS changes only; never discard reservations or force deletion success."""
        def mutate(rows, now):
            if action == "resume-admission":
                if sum(k.startswith("w:") for k in rows) > self.limits.max_total:
                    raise ValueError("Cannot resume above maximum capacity")
                rows["control"] = {"admission_paused": False}
                return {"admission_paused": False}
            w = rows.get("w:" + wid)
            if not w or w["lifetime"] != expected_lifetime:
                raise PermissionError("Explicit matching worker/lifetime required")
            if w.get("claim_until", 0) > now or w.get("delete_after", 0) > now:
                raise PermissionError("Worker still has a live claim or in-flight operation")
            if action == "retry-provisioning":
                if w["state"] != "quarantined":
                    raise PermissionError("Only quarantined provisioning may be retried")
                w.update(state="provisioning", created=now)
            elif action == "destroy":
                if w["state"] in ("active", "activating"):
                    raise PermissionError("Release the active session first")
                # Explicit operator acknowledges late-create uncertainty before requesting cleanup.
                w.update(state="draining", delete_after=now,
                         delete_operation=w.get("delete_operation", uuid.uuid4().hex))
            else:
                raise ValueError("Unknown administrative recovery action")
            w.pop("claim", None)
            w.pop("claim_until", None)
            return {"worker_id": wid, "state": w["state"]}
        return self.change(mutate)
