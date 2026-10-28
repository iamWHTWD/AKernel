"""Narrow adapters for the operator HTTP listener and scaler handoff ConfigMap."""

from __future__ import annotations

import json
from urllib.parse import quote

from .engine import digest, op_id
from .errors import Blocked, Conflict, UnknownOutcome, UpgradeError


class Management:
    BASE = "/v1/node-upgrades"

    def __init__(self, http):
        self.http = http

    def capabilities(self):
        return self.http.request("GET", self.BASE + "/capabilities")

    def status(self, state):
        return self.http.request("GET", self.BASE + "/tasks/" + quote(state["task_id"], safe=""))

    def action(self, state, action, node_id="", **extra):
        node = next((n for n in state["nodes"] if n["node_id"] == node_id), {})
        payload = {"task_id": state["task_id"], "epoch": state["epoch"],
                   "node_id": node_id, "pod_uid": node.get("new_uid", node.get("old_uid", "")),
                   "node_session_id": node.get("node_session_id", "")}
        if node_id:
            inspection = self.inspect(node_id)
            payload["node_session_id"] = inspection.get("node_session_id", "")
            if not payload["pod_uid"]:
                payload["pod_uid"] = inspection.get("pod_uid", "")
        payload.update(extra)
        if state.get("phase") == "Aborting":
            payload["rollback"] = True
        payload.setdefault("operation_id", op_id(state["task_id"], node_id, action + digest(payload)))
        value = self.http.request("POST", self.BASE + "/tasks/" + quote(state["task_id"], safe="")
                                  + "/" + action, payload)
        if value.get("unknown") is True or value.get("outcome") == "unknown":
            raise UnknownOutcome("management operation remains unknown")
        return value

    def begin(self, state):
        return self.http.request("POST", self.BASE + "/tasks", {
            "task_id": state["task_id"], "epoch": state["epoch"],
            "operation_id": op_id(state["task_id"], "", "begin"),
            "namespace": state["namespace"], "daemonset_uid": state["daemonset_uid"],
            "target_image": state["image"], "nodes": [
                {"node_id": n["node_id"], "pod_uid": n["old_uid"],
                 "node_session_id": n.get("node_session_id", "")} for n in state["nodes"]]})

    def prepare(self, state, node_id):
        node = next(n for n in state["nodes"] if n["node_id"] == node_id)
        return self.action(state, "prepare-node", node_id,
                           register_source=node.get("register_source", False),
                           node_session_id=node["node_session_id"])

    def migrate(self, state, node_id, record, operation_id, **extra):
        value = self.action(state, "migrate", node_id,
                            environment_id=record["instanceID"], operation_id=operation_id, **extra)
        if value.get("completed") is True and value.get("durability") == "Published":
            value["outcome"] = "completed"
        return value

    def inspect(self, node_id):
        return self.http.request("GET", self.BASE + "/nodes/" + quote(node_id, safe="") + "/inspect")

    def quiesce(self, state, node_id):
        value = self.action(state, "quiesce", node_id)
        return value.get("inspection", value)

    def confirm_delete(self, state, node_id, proof, pod):
        if (proof.get("strict_empty") is not True or not proof.get("permit_id")
                or proof.get("pod_uid") != pod["metadata"]["uid"]):
            raise Blocked("current Pod has no strict-empty delete permit")
        return self.action(state, "quiesce", node_id, permit_id=proof["permit_id"],
                           pod_uid=pod["metadata"]["uid"],
                           pod_resource_version=pod["metadata"]["resourceVersion"], delete_intent=True)

    def canary(self, state, node_id, uid, source_node_id, source_pod_uid, storage_alias, namespace, attempt=0):
        task = self.status(state)
        expected = {key: state[key] for key in ("task_id", "epoch", "namespace", "daemonset_uid")}
        expected["target_image"] = state["image"]
        if (not isinstance(task, dict) or any(task.get(key) != value for key, value in expected.items())
                or task.get("phase") not in ("Active", "Aborting")
                or not isinstance(task.get("nodes"), dict)):
            raise Conflict("canary authorization task identity/phase changed")
        target = task["nodes"].get(node_id)
        if target is not None and not isinstance(target, dict):
            raise UnknownOutcome("canary target binding is invalid")
        revision = 0 if target is None else target.get("control_revision")
        if type(revision) is not int or revision < 0:
            raise UnknownOutcome("canary target control revision is unavailable")
        environment_id = "upgrade-canary-" + digest([state["task_id"], node_id, uid, attempt])[:24]
        value = self.action(state, "authorize-canary", source_node_id, pod_uid=source_pod_uid,
                            environment_id=environment_id, generation=0,
                            target_node_id=node_id, target_pod_uid=uid,
                            expected_target_control_revision=revision,
                            environment_namespace=namespace,
                            checkpoint_storage_alias=storage_alias,
                            actions=["create", "resume", "recover", "route"])
        value.update(source_node_id=source_node_id, target_node_id=node_id,
                     checkpoint_storage_alias=storage_alias)
        return value

    def verify(self, state, node_id, uid, proof):
        return self.action(state, "verify-node", node_id, pod_uid=uid,
                           permit_id=proof.get("permit_id"), revision=proof.get("revision"))

    def revoke_canary(self, state, environment_id):
        return self.action(state, "revoke-canary", environment_id=environment_id)

    def revoke_delete(self, state, node_id, pod, expected_delete_resource_version):
        return self.action(state, "revoke-delete", node_id, pod_uid=pod["metadata"]["uid"],
                           pod_resource_version=pod["metadata"]["resourceVersion"],
                           expected_delete_resource_version=expected_delete_resource_version)

    def freeze(self, state):
        return self.action(state, "abort", stage="freeze")

    def rollback(self, state, node_id, pod):
        value = self.action(state, "quiesce", node_id, rollback=True,
                            pod_uid=pod["metadata"]["uid"],
                            pod_resource_version=pod["metadata"]["resourceVersion"])
        return value.get("inspection", value)

    def restore_original(self, state, node_id, pod):
        node = next(n for n in state["nodes"] if n["node_id"] == node_id)
        registration = ({"register_source": True, "node_session_id": node["node_session_id"]}
                        if node.get("register_source") else {})
        return self.action(state, "abort", node_id, stage="restore-original",
                           pod_uid=pod["metadata"]["uid"],
                           pod_resource_version=pod["metadata"]["resourceVersion"],
                           **registration)

    def finish(self, state, outcome, **extra):
        return self.action(state, "complete" if outcome == "Completed" else "abort",
                           stage="finalize", template_restored=outcome == "Aborted", **extra)


class Scaling:
    """Own only request.json; preserve scaler-owned status.json with RV CAS."""
    def __init__(self, kube, name, cluster_id, pool_id, count=1):
        if not all((name, cluster_id, pool_id)) or count < 1:
            raise Blocked("scaler ConfigMap, cluster_id, pool_id and positive count required")
        self.kube, self.name, self.cluster_id = kube, name, cluster_id
        self.pool_id, self.count = pool_id, count

    def configuration(self):
        return {"name": self.name, "cluster_id": self.cluster_id,
                "pool_id": self.pool_id, "count": self.count}

    def _read(self):
        obj = self.kube.get("configmaps", self.name)
        data = obj.get("data", {})
        try:
            request = json.loads(data.get("request.json", "null"))
            status = json.loads(data.get("status.json", "null"))
        except ValueError as error:
            raise UpgradeError("invalid scaler handoff ConfigMap") from error
        return obj, request, status

    def _write(self, obj, request):
        path = "/data/request.json"
        if "data" not in obj:
            operations = [{"op": "add", "path": "/data", "value": {
                "request.json": json.dumps(request, sort_keys=True)}}]
        else:
            operations = [{"op": "add", "path": path, "value": json.dumps(request, sort_keys=True)}]
        return self.kube.patch("configmaps", obj, operations)

    def _observed(self, status, state, handoff):
        return (isinstance(status, dict) and status.get("version") == 1
                and status.get("cluster_id") == self.cluster_id
                and status.get("observed_task_id") == state["task_id"]
                and status.get("observed_epoch") == state["epoch"]
                and status.get("handoff") == handoff)

    def _request(self, state, previous, status):
        if previous is not None:
            if previous.get("cluster_id") != self.cluster_id:
                raise Conflict("scaler cluster identity mismatch")
            if previous.get("task_id") != state["task_id"]:
                if previous.get("phase") != "Released":
                    raise Conflict("another task retains scaler protection")
                if not self._observed(status, previous, "Released"):
                    raise Blocked("previous scaler task has not acknowledged release")
                previous = None
            elif previous.get("epoch") != state["epoch"]:
                raise Conflict("scaler epoch mismatch; reconcile takeover explicitly")
        return previous or {"version": 1, "cluster_id": self.cluster_id,
                            "task_id": state["task_id"], "epoch": state["epoch"],
                            "phase": "Active", "capacity_requests": []}

    def status(self, state):
        _, _, status = self._read()
        if (not isinstance(status, dict) or status.get("version") != 1
                or status.get("cluster_id") != self.cluster_id
                or status.get("observed_task_id") != state["task_id"]
                or status.get("observed_epoch") != state["epoch"]):
            raise Blocked("scaler has not observed this task and epoch")
        if status.get("handoff") != "Ready":
            raise Blocked("scaler handoff is not Ready; scale-down guard retained")
        return status

    def protect(self, state):
        obj, previous, status = self._read()
        request = self._request(state, previous, status)
        if request.get("phase") == "Released":
            raise Conflict("cannot reactivate a released scaler task")
        if previous != request:
            self._write(obj, request)
        return self.status(state)

    def ready_nodes(self, state, *, include_pending=False):
        status = self.status(state)
        nodes = list(status.get("observed_nodes", []))
        for request in status.get("capacity_requests", []):
            if include_pending or request.get("phase") in ("ReadyForValidation", "Completed"):
                nodes.extend(request.get("nodes", []))
        result = {}
        for node in nodes:
            if include_pending or node.get("ready_for_validation") is True:
                if not all(node.get(key) for key in ("name", "uid", "pool_id", "instance_id")):
                    if include_pending and node.get("ready_for_validation") is not True:
                        continue  # Future cloud members may not have a Kubernetes identity yet.
                    raise UpgradeError("incomplete attributed expansion node identity")
                result[(node["name"], node["uid"])] = node
        return list(result.values())

    def finalize(self, state):
        obj, previous, status = self._read()
        request = self._request(state, previous, status)
        if not self._observed(status, state, "Ready"):
            raise Blocked("scaler has not observed this task's handoff")
        if state.get("phase") != "Aborting":
            receipts = {item["request_id"]: item for item in status.get("capacity_requests", [])}
            for expected in request["capacity_requests"]:
                receipt = receipts.get(expected["request_id"], {})
                if (receipt.get("phase") != "Completed"
                        or any(receipt.get(key) != value for key, value in expected.items())):
                    raise Blocked("accepted capacity request has not completed")
        # Abort no longer needs undelivered capacity. Fence further purchases,
        # then await every submitted/unknown expansion through unsettled_up.
        if request["phase"] == "Active":
            request["phase"] = "Finalizing"
            self._write(obj, request)
        elif request["phase"] != "Finalizing":
            raise Conflict("released scaler task cannot finalize")
        if status.get("observed_phase") != "Finalizing" or status.get("unsettled_up") is not False:
            raise Blocked("waiting for Finalizing acknowledgment and attributed expansions")

    def request_capacity(self, state, node, need, free):
        return self._capacity_request(state, [state["task_id"], state["epoch"], node["node_id"]],
                                      repeat=True)

    def prewarm_capacity(self, state):
        # This one task-level request is reused even after Completed, including
        # when the Job exits before saving any subsequent local progress.
        return self._capacity_request(state,
            [state["task_id"], state["epoch"], "prewarm", self.pool_id], repeat=False)

    def _capacity_request(self, state, basis, *, repeat):
        obj, previous, status = self._read()
        request = self._request(state, previous, status)
        if (request["phase"] != "Active" and (repeat or request["phase"] != "Finalizing")
                or not self._observed(status, state, "Ready")):
            raise Blocked("capacity requires this task's active scaler handoff")
        requests = {item["request_id"]: item for item in request["capacity_requests"]}
        receipts = {item["request_id"]: item for item in status.get("capacity_requests", [])}
        round_number = 0
        while True:
            # Keep round zero compatible with existing persisted requests. Later
            # rounds are derived from the channel, with no second operation log.
            identity = "upgrade-capacity-" + digest(basis if round_number == 0 else basis + [round_number])[:20]
            entry = {"request_id": identity, "pool_id": self.pool_id, "count": self.count}
            old = requests.get(identity)
            if old is None:
                if request["phase"] != "Active":
                    raise Blocked("finalizing scaler capacity requests cannot be extended")
                request["capacity_requests"].append(entry)
                self._write(obj, request)
                return
            if old != entry:
                raise Conflict("capacity request ID was reused with different contents")
            receipt = receipts.get(identity)
            if receipt is None:
                return  # Already appended, but not yet observed after a crash/CAS.
            if any(receipt.get(key) != value for key, value in entry.items()):
                raise Conflict("capacity receipt identity differs from its request")
            if receipt.get("phase") != "Completed":
                return receipt  # Pending, blocked or unknown capacity is never bought twice.
            if not repeat:
                return receipt
            round_number += 1

    def release(self, state):
        obj, previous, status = self._read()
        request = self._request(state, previous, status)
        if request["phase"] != "Released":
            request["phase"] = "Released"
            self._write(obj, request)
        if not self._observed(status, state, "Released"):
            raise Blocked("scaler has not acknowledged this task's release")
