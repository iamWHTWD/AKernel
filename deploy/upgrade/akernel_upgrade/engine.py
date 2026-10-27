"""Persistent serial execution. Every destructive action follows a durable intent."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass

from .errors import Blocked, Conflict, UnknownOutcome, UpgradeError


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def op_id(task: str, node: str, action: str) -> str:
    return "upgrade-" + digest([task, node, action])[:32]


@dataclass(frozen=True)
class Settings:
    namespace: str
    daemonset: str
    image: str
    task_id: str
    executor_id: str
    container: str = "akernel-node"
    pool_id: str = ""
    poll_seconds: int = 5
    capacity_wait_seconds: int = 60
    node_timeout_seconds: int = 600
    canary_cpu: int = 1000
    canary_memory: int = 1024
    checkpoint_storage_alias: str = ""

    def validate(self):
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", self.image):
            raise Blocked("new image must use an explicit immutable @sha256 digest")
        if not all((self.namespace, self.daemonset, self.task_id, self.executor_id)):
            raise Blocked("namespace, daemonset, task_id and executor_id are required")
        if not self.checkpoint_storage_alias or self.checkpoint_storage_alias == "local":
            raise Blocked("an explicit shared checkpoint storage alias is required")
        if (self.poll_seconds <= 0 or self.capacity_wait_seconds < 60
                or self.node_timeout_seconds < self.capacity_wait_seconds):
            raise Blocked("invalid polling/deadline settings; capacity request delay must be >=60s")


class Executor:
    """Drivers are injected, so protocol and failure behavior can be tested independently."""
    REQUIRED = ("planned_migration", "paused_transfer", "quiesce", "session_isolation", "canary",
                "canary_cleanup")

    def __init__(self, settings, kube, inventory, management, canary, scaling,
                 *, execution_identity=None, clock=time.monotonic, sleep=time.sleep):
        settings.validate()
        self.cfg, self.kube, self.inventory = settings, kube, inventory
        self.management, self.canary, self.scaling = management, canary, scaling
        self.execution_identity = copy.deepcopy(execution_identity)
        self.clock, self.sleep = clock, sleep
        self.saved = None
        self.state = None

    def state_name(self, uid):
        # One CAS slot per DaemonSet, not per Job: concurrent task IDs conflict.
        return "akernel-node-upgrade-" + digest([self.cfg.namespace, uid])[:20]

    def template(self, ds):
        value = copy.deepcopy(ds["spec"]["template"])
        containers = value["spec"]["containers"]
        indexes = [i for i, c in enumerate(containers) if c["name"] == self.cfg.container]
        if len(indexes) != 1:
            raise Blocked("exactly one configured node container is required")
        index = indexes[0]
        image = containers[index]["image"]
        containers[index]["image"] = "<node-image>"
        # This is the sole configuration addition permitted by the task. It
        # closes admission before a replacement process can register.
        env = containers[index].get("env", [])
        containers[index]["env"] = [e for e in env if e.get("name") != "ADX_NODE_UPGRADE_ISOLATED"]
        return digest(value), image, index

    def snapshot(self):
        # Resource and instance failures never mean empty or zero demand.
        resources = self.inventory.resources()
        instances = self.inventory.instances()
        ds = self.kube.get("daemonsets", self.cfg.daemonset)
        if ds.get("apiVersion") != "apps/v1" or ds.get("kind") != "DaemonSet":
            raise Blocked("only native apps/v1 DaemonSet is supported")
        pods = self.kube.list("pods", ds["metadata"]["uid"])
        nodes = {}
        for pod in pods:
            node = pod.get("spec", {}).get("nodeName")
            if not node or node in nodes or pod["metadata"].get("deletionTimestamp"):
                raise Blocked("pending/unbound, duplicate or terminating node Pod requires inspection")
            nodes[node] = pod
        if len(nodes) < 2:
            raise Blocked("cross-node upgrade requires at least two node Pods")
        return ds, nodes, resources, instances

    def source_snapshot(self, name, pod, resources):
        status = next((item for item in pod.get("status", {}).get("containerStatuses", [])
                       if item.get("name") == self.cfg.container), {})
        if (pod["metadata"].get("deletionTimestamp") or status.get("ready") is not True
                or not status.get("imageID", "").endswith(self.state["old_image"].split("@", 1)[1])):
            raise Blocked("source node Pod must be ready with the pinned old imageID")
        resource = resources.get(name)
        if resource is None:
            raise Blocked("node missing from authoritative resource inventory")
        session = self.management.inspect(name).get("node_session_id")
        if not session:
            raise Blocked("source node must expose its upgrade-aware process identity")
        return {"node_id": name, "old_uid": pod["metadata"]["uid"], "node_session_id": session,
                "pod_name": pod["metadata"]["name"], "phase": "Pending",
                "need": {key: max(0, resource["capacity"].get(key, 0)
                                 - resource["allocatable"].get(key, 0))
                         for key in ("CPU", "Memory", "Disk")}}

    def freeze_baseline(self):
        if self.state.get("baseline_frozen"):
            return
        ds, pods, resources, _ = self.snapshot()
        fingerprint, image, _ = self.template(ds)
        if (ds["metadata"]["uid"] != self.state["daemonset_uid"]
                or fingerprint != self.state["template_hash"] or image != self.state["old_image"]):
            raise Conflict("node template changed before baseline handoff")
        observed = {name: self.source_snapshot(name, pod, resources) for name, pod in sorted(pods.items())}
        for node in self.state["nodes"]:
            current = observed.pop(node["node_id"], {})
            if any(current.get(key) != node[key] for key in ("old_uid", "node_session_id")):
                raise Conflict("original node Pod/session changed before baseline handoff")
        self.state["nodes"].extend(observed.values())
        self.state["baseline_frozen"] = True
        self.save()

    def plan(self):
        ds, pods, resources, instances = self.snapshot()
        fingerprint, old_image, _ = self.template(ds)
        capabilities = self.management.capabilities()
        missing = [name for name in self.REQUIRED if capabilities.get(name) is not True]
        if capabilities.get("version") != 1:
            missing.append("management protocol version 1")
        return {"read_only": True, "daemonset_uid": ds["metadata"]["uid"],
                "template_hash": fingerprint, "old_image": old_image, "image": self.cfg.image,
                "nodes": [{"node_id": node, "pod_uid": pod["metadata"]["uid"],
                           "instances": sum(i["node_id"] == node for i in instances),
                           "in_resources": node in resources} for node, pod in sorted(pods.items())],
                "blockers": missing + self.canary.preflight()}

    def save(self):
        self.saved = self.kube.save_state(self.saved, self.state)

    def task_configuration(self):
        fields = ("namespace", "daemonset", "image", "container", "pool_id",
                  "checkpoint_storage_alias", "canary_cpu", "canary_memory")
        configuration = {**{key: getattr(self.cfg, key) for key in fields},
                         "scaler": self.scaling.configuration()}
        if self.execution_identity is not None:
            configuration["execution_identity"] = copy.deepcopy(self.execution_identity)
        return configuration

    def check_configuration(self):
        if self.state.get("configuration") != self.task_configuration():
            raise Conflict("immutable task configuration changed or was not recorded")

    def load(self, *, check_executor=True):
        ds = self.kube.get("daemonsets", self.cfg.daemonset)
        self.saved = self.kube.get("configmaps", self.state_name(ds["metadata"]["uid"]))
        self.state = json.loads(self.saved["data"]["state.json"])
        if self.state["task_id"] != self.cfg.task_id:
            raise Conflict("DaemonSet belongs to another task")
        if check_executor and self.state["executor_id"] != self.cfg.executor_id:
            raise Conflict("executor identity changed; explicit confirmed takeover is required")
        if check_executor:
            self.check_configuration()
        return self.state

    def apply(self):
        plan = self.plan()
        if plan["blockers"]:
            raise Blocked("upgrade prerequisites unavailable: " + ", ".join(plan["blockers"]))
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", plan["old_image"]):
            raise Blocked("old image must be digest-pinned for verifiable rollback")
        ds, nodes, resources, _ = self.snapshot()
        _, _, container_index = self.template(ds)
        old_env = ds["spec"]["template"]["spec"]["containers"][container_index].get("env", [])
        self.state = {"version": 1, "namespace": self.cfg.namespace,
                      "task_id": self.cfg.task_id, "executor_id": self.cfg.executor_id,
                      "daemonset_uid": plan["daemonset_uid"], "template_hash": plan["template_hash"],
                      "old_image": plan["old_image"], "image": self.cfg.image,
                      "phase": "Starting", "epoch": 1, "index": 0, "nodes": [],
                      "capacity_requested": False, "last_error": None,
                      "configuration": self.task_configuration()}
        self.state["original_bootstrap_env"] = next(
            (e for e in old_env if e.get("name") == "ADX_NODE_UPGRADE_ISOLATED"), None)
        for name, pod in sorted(nodes.items()):
            self.state["nodes"].append(self.source_snapshot(name, pod, resources))
        name = self.state_name(plan["daemonset_uid"])
        try:
            self.saved = self.kube.create_configmap(name, {"state.json": json.dumps(self.state, sort_keys=True)})
        except Conflict:
            old = self.kube.get("configmaps", name)
            previous = json.loads(old["data"]["state.json"])
            if (previous.get("phase") not in ("Completed", "Aborted")
                    or previous.get("task_id") == self.cfg.task_id):
                raise Conflict("DaemonSet has an active or reused task identity")
            self.saved = self.kube.save_state(old, self.state)
        return self.run()

    def run(self):
        if self.state is None:
            self.load()
        self.check_configuration()
        if self.reconcile_terminal():
            return self.state
        if self.state["phase"] in ("Completed", "Aborted"):
            self.wait_scaler(release=True)
            return self.state
        if self.state["phase"] == "Aborting":
            raise Blocked("abort is in progress; continue abort rather than republishing the new image")
        try:
            self.wait_scaler()
            self.freeze_baseline()
            observed = self.management.begin(self.state)
            if self.reconcile_terminal(observed):
                return self.state
            self.publish(self.cfg.image)
            self.state["phase"] = "Running"
            self.save()
            while True:
                while self.state["index"] < len(self.state["nodes"]):
                    node = self.state["nodes"][self.state["index"]]
                    self.upgrade_node(node)
                    self.state["index"] += 1
                    self.save()
                if self.finish_scaling():
                    break
            self.restore_bootstrap("Completed", scaling_finalized=True)
            self.management.finish(self.state, "Completed")
            self.state["phase"] = "Completed"
            self.save()
            self.wait_scaler(release=True)
            return self.state
        except UpgradeError as error:
            if self.state["phase"] not in ("Completed", "Aborted"):
                self.state["phase"] = "NeedsAttention"
            self.state["last_error"] = {"kind": type(error).__name__, "message": str(error)}
            self.save()
            raise

    def reconcile_terminal(self, observed=None):
        # Before the first publish there may legitimately be no ADX task yet.
        # Every completed/aborted batch has persisted a template generation.
        if observed is None:
            if (self.state["phase"] in ("Completed", "Aborted")
                    or "template_generation" not in self.state):
                return False
            observed = self.management.status(self.state)
        if not isinstance(observed, dict):
            raise UnknownOutcome("authoritative upgrade task is unavailable")
        expected = {key: self.state[key] for key in
                    ("task_id", "epoch", "namespace", "daemonset_uid")}
        expected["target_image"] = self.state["image"]
        if any(observed.get(key) != value for key, value in expected.items()):
            raise Conflict("authoritative upgrade task identity changed")
        if observed.get("phase") not in ("Completed", "Aborted"):
            return False
        # Terminal ADX evidence survives reply/local-save loss. Its released
        # gates must never be revalidated or reopened under this old batch.
        self.state["phase"] = observed["phase"]
        self.save()
        self.wait_scaler(release=True)
        return True

    def final_inventory(self, outcome):
        pods = self.kube.list("pods", self.state["daemonset_uid"])
        expected = {n["node_id"]: n.get("new_uid") for n in self.state["nodes"] if n["phase"] == "Done"}
        for name, record in self.state.get("validated_capacity_nodes", {}).items():
            expected[name] = record["pod_uid"]
        if outcome == "Aborted":
            for node in self.state["nodes"]:
                if node["phase"] in ("Pending", "RestoredOriginal"):
                    expected[node["node_id"]] = node["old_uid"]
        seen = set()
        for pod in pods:
            node = pod.get("spec", {}).get("nodeName")
            if (not node or node in seen or pod["metadata"].get("deletionTimestamp")
                    or expected.get(node) != pod["metadata"]["uid"]):
                raise Blocked("fresh owner inventory contains an unverified/replaced/terminating Pod")
            seen.add(node)
        if seen != set(expected):
            raise Blocked("fresh owner inventory is missing an expected verified Pod")
        return [{"pod_uid": p["metadata"]["uid"], "node_id": p["spec"]["nodeName"]} for p in pods]

    def restore_bootstrap(self, outcome, *, scaling_finalized=False):
        if not scaling_finalized and not self.finish_scaling():
            raise Blocked("new old-image source requires queue reconciliation before finalization")
        self.final_inventory(outcome)
        ds, _, index = self.checked_ds()
        env = ds["spec"]["template"]["spec"]["containers"][index].get("env", [])
        restored = [e for e in env if e.get("name") != "ADX_NODE_UPGRADE_ISOLATED"]
        if self.state.get("original_bootstrap_env"):
            restored.append(self.state["original_bootstrap_env"])
        if env != restored:
            ds = self.kube.patch("daemonsets", ds, [{"op": "add",
                "path": f"/spec/template/spec/containers/{index}/env", "value": restored}])
        self.wait_observed(ds)
        # Fresh owner inventory is evidence supplied to the authoritative
        # finalization. Unknown/Pending/duplicate Pods are never omitted.
        self.state["final_pods"] = self.final_inventory(outcome)
        self.save()

    def wait_scaler(self, *, release=False):
        deadline = self.clock() + self.cfg.node_timeout_seconds
        action = self.scaling.release if release else self.scaling.protect
        while True:
            try:
                action(self.state)
                return
            except Blocked:
                if self.clock() >= deadline:
                    raise
                self.sleep(self.cfg.poll_seconds)

    def finish_scaling(self):
        deadline = self.clock() + self.cfg.node_timeout_seconds
        while True:
            pending_old = self.validate_capacity_nodes(include_pending=True)
            if self.state.get("phase") == "Aborting":
                if any(n["phase"] not in ("Done", "RestoredOriginal") for n in self.state["nodes"]):
                    return False
            elif self.state["index"] < len(self.state["nodes"]):
                return False
            if pending_old:
                if self.clock() >= deadline:
                    raise Blocked("attributed old-image Pod is not ready; finalization held")
                self.sleep(self.cfg.poll_seconds)
                continue
            try:
                self.scaling.finalize(self.state)
                return True
            except Blocked:
                if self.clock() >= deadline:
                    raise
                self.sleep(self.cfg.poll_seconds)

    def checked_ds(self):
        ds = self.kube.get("daemonsets", self.cfg.daemonset)
        fingerprint, image, index = self.template(ds)
        if (ds["metadata"]["uid"] != self.state["daemonset_uid"]
                or fingerprint != self.state["template_hash"]):
            raise Conflict("DaemonSet identity/configuration drift")
        if image not in (self.state["old_image"], self.state["image"]):
            raise Conflict("node image changed outside task")
        return ds, image, index

    def publish(self, image):
        ds, current, index = self.checked_ds()
        if ds["spec"].get("updateStrategy", {}).get("type") != "OnDelete":
            # Persist the intended change before the API call. A lost reply is
            # reconciled on resume rather than executing the following mutation.
            self.state["template_intent"] = "OnDelete"
            self.save()
            self.kube.patch("daemonsets", ds, [{"op": "add", "path": "/spec/updateStrategy",
                                               "value": {"type": "OnDelete"}}])
            ds, current, index = self.checked_ds()
            if ds["spec"].get("updateStrategy", {}).get("type") != "OnDelete":
                raise UnknownOutcome("OnDelete change is not confirmed")
        self.wait_observed(ds)
        env = ds["spec"]["template"]["spec"]["containers"][index].get("env", [])
        marker = next((e for e in env if e.get("name") == "ADX_NODE_UPGRADE_ISOLATED"), None)
        if marker != {"name": "ADX_NODE_UPGRADE_ISOLATED", "value": "true"}:
            if marker is not None:
                raise Conflict("unexpected startup-isolation environment configuration")
            ds = self.kube.patch("daemonsets", ds, [{"op": "add",
                "path": f"/spec/template/spec/containers/{index}/env",
                "value": env + [{"name": "ADX_NODE_UPGRADE_ISOLATED", "value": "true"}]}])
            self.wait_observed(ds)
        if current != image:
            self.state["template_intent"] = image
            self.save()
            ds = self.kube.patch("daemonsets", ds, [{"op": "replace",
                "path": f"/spec/template/spec/containers/{index}/image", "value": image}])
        self.wait_observed(ds)
        self.state["template_generation"] = ds["metadata"]["generation"]
        self.state["template_intent"] = None
        self.save()

    def wait_observed(self, ds):
        generation = ds["metadata"]["generation"]
        deadline = self.clock() + self.cfg.node_timeout_seconds
        while True:
            fresh, _, _ = self.checked_ds()
            if fresh["metadata"]["generation"] != generation:
                raise Conflict("DaemonSet generation changed during controller confirmation")
            if fresh.get("status", {}).get("observedGeneration", 0) >= generation:
                return
            if self.clock() >= deadline:
                raise Blocked("DaemonSet generation has not been observed")
            self.sleep(self.cfg.poll_seconds)

    def capacity(self, node):
        started = self.clock()
        next_request = started + self.cfg.capacity_wait_seconds
        while True:
            self.validate_capacity_nodes()
            _, _, resources, _ = self.snapshot()
            source = resources.get(node["node_id"])
            if source is None:
                raise Blocked("source resource accounting is unavailable")
            # Earlier migrations can add workload to a later source node. Its
            # apply-time accounting is no longer a valid evacuation estimate.
            if source["status"] == 0:
                node["need"] = {key: source["capacity"].get(key, 0) - source["allocatable"].get(key, 0)
                                for key in ("CPU", "Memory", "Disk")}
            else:
                # Public resources deliberately projects paused nodes as zero.
                # Use the private scheduling ledger without changing admission.
                try:
                    raw = self.management.inspect(node["node_id"]).get("resources")
                except UpgradeError as error:
                    raise Blocked("paused source management resource ledger is unavailable") from error
                if (not isinstance(raw, dict) or not isinstance(raw.get("capacity"), dict)
                        or not isinstance(raw.get("allocatable"), dict)):
                    raise Blocked("paused source management resource ledger is missing")
                occupied = {}
                for label, field, unit in (("CPU", "cpu_millis", 1),
                                           ("Memory", "memory_bytes", 1024 * 1024),
                                           ("Disk", "disk_bytes", 1024 * 1024)):
                    capacity, available = raw["capacity"].get(field), raw["allocatable"].get(field)
                    if (type(capacity) is not int or type(available) is not int
                            or not 0 <= available <= capacity):
                        raise Blocked("paused source management resource ledger is invalid")
                    occupied[label] = (capacity - available + unit - 1) // unit
                node["need"] = occupied
            free = {key: 0 for key in ("CPU", "Memory", "Disk")}
            for identity, resource in resources.items():
                if identity != node["node_id"] and resource["status"] == 0:
                    for key in free:
                        free[key] += resource["allocatable"].get(key, 0)
            need = dict(node["need"])
            need["CPU"] += self.cfg.canary_cpu
            need["Memory"] += self.cfg.canary_memory
            if all(free[key] >= need[key] for key in free):
                return
            if self.clock() - started >= self.cfg.node_timeout_seconds:
                raise Blocked("capacity remains insufficient; request and protections retained")
            if self.clock() >= next_request:
                # The adapter reuses an unfinished request. A later round is
                # allowed only after its predecessor Completed and this fresh gap.
                self.scaling.request_capacity(self.state, node, need, free)
                if not self.state["capacity_requested"]:
                    self.state["capacity_requested"] = True
                    self.save()
                next_request = self.clock() + self.cfg.capacity_wait_seconds
            self.sleep(self.cfg.poll_seconds)

    def check_node_identity(self, name, uid, pool, instance):
        current = self.kube.get_node(name)
        metadata = current.get("metadata", {})
        provider = current.get("spec", {}).get("providerID", "").removeprefix("alicloud://")
        if (metadata.get("uid") != uid or metadata.get("deletionTimestamp")
                or metadata.get("labels", {}).get("alibabacloud.com/nodepool-id") != pool
                or not instance or not provider.endswith(("." + instance, ":" + instance))):
            raise Conflict("attributed capacity Node UID/pool/instance changed")

    def validate_capacity_nodes(self, *, include_pending=False):
        validated = self.state.setdefault("validated_capacity_nodes", {})
        pending_old = False
        for node in self.scaling.ready_nodes(self.state, include_pending=include_pending):
            name = node["name"]
            self.check_node_identity(name, node["uid"], node.get("pool_id"), node.get("instance_id"))
            matches = [p for p in self.kube.list("pods", self.state["daemonset_uid"])
                       if p.get("spec", {}).get("nodeName") == name]
            if not matches and include_pending and node.get("ready_for_validation") is not True:
                continue
            if len(matches) != 1 or matches[0]["metadata"].get("deletionTimestamp"):
                raise Blocked("capacity node has no unique node Pod")
            pod = matches[0]
            source = next((initial for initial in self.state["nodes"] if initial["node_id"] == name), None)
            if source is not None:
                if ((source.get("node_uid") and source["node_uid"] != node["uid"])
                        or pod["metadata"]["uid"] not in (source["old_uid"], source.get("new_uid"))):
                    raise Conflict("queued source Pod changed outside the upgrade")
                continue
            previous = validated.get(name, {})
            if previous.get("node_uid") == node["uid"] and previous.get("pod_uid") == pod["metadata"]["uid"]:
                continue
            status = next((s for s in pod.get("status", {}).get("containerStatuses", [])
                           if s.get("name") == self.cfg.container), {})
            old_image = self.state.get("old_image")
            old = (old_image and old_image != self.cfg.image
                   and status.get("imageID", "").endswith(old_image.split("@", 1)[1]))
            if status.get("ready") is not True or node.get("ready_for_validation") is not True:
                declared = next((c.get("image") for c in pod.get("spec", {}).get("containers", [])
                                 if c.get("name") == self.cfg.container), None)
                pending_old |= bool(old or (old_image and old_image != self.cfg.image and declared == old_image))
                continue
            if old:
                source = self.source_snapshot(name, pod, self.inventory.resources())
                source.update(register_source=True, node_uid=node["uid"], pool_id=node["pool_id"],
                              instance_id=node["instance_id"])
                self.state["nodes"].append(source)
                self.save()
                continue
            if not status.get("imageID", "").endswith(self.cfg.image.split("@", 1)[1]):
                raise Blocked("capacity node actual imageID differs from task template")
            self.validate_node(name, pod["metadata"]["uid"])
            validated[name] = {"node_uid": node["uid"], "pod_uid": pod["metadata"]["uid"]}
            self.save()
        return pending_old

    def validate_node(self, identity, uid):
        validations = self.state.setdefault("validations", {})
        record = validations.get(identity)
        if record is None or record["pod_uid"] != uid:
            if record and record.get("grant"):
                self.cleanup_canary(record["grant"])
            record = {"pod_uid": uid, "attempt": 0, "phase": "Authorizing"}
            validations[identity] = record
            self.save()
        if record["phase"] == "Verified":
            current = self.management.inspect(identity)
            if (current.get("pod_uid") == uid and current.get("task_id") == self.state["task_id"]
                    and current.get("epoch") == self.state["epoch"]
                    and current.get("node_session_id") == record["node_session_id"]
                    and str(current.get("phase", "")).lower() == "verified"):
                self.management.verify(self.state, identity, uid, current)
                return
            raise Blocked("verified node session/gate changed; previous probe cannot be reused")
        if record["phase"] == "Verifying":
            current = self.management.inspect(identity)
            if (current.get("pod_uid") == uid and current.get("task_id") == self.state["task_id"]
                    and current.get("epoch") == self.state["epoch"]
                    and current.get("node_session_id") == record["node_session_id"]
                    and str(current.get("phase", "")).lower() == "verified"):
                self.management.verify(self.state, identity, uid, current)
                record["phase"] = "Verified"
                self.save()
                return
        if record["phase"] == "Probing":
            # A crashed probe is never retried with a Deleted identity. Before
            # allocating a new persistent attempt, explicitly reconcile deletion
            # and revoke the old target's grants through strict Quiescing.
            self.cleanup_canary(record["grant"])
            proof = self.management.quiesce(self.state, identity)
            self.validate_empty(proof, uid)
            record["attempt"] += 1
            record["phase"] = "Authorizing"
            self.save()
        if record["phase"] in ("Probed", "Verifying"):
            self.cleanup_canary(record["grant"])
            current = self.management.inspect(identity)
            if current.get("node_session_id") != record["node_session_id"]:
                raise Blocked("node session changed after successful probe; reconcile isolation before another attempt")
            proof = self.management.quiesce(self.state, identity)
            self.validate_empty(proof, uid)
            record["phase"] = "Verifying"
            self.save()
            self.management.verify(self.state, identity, uid, proof)
            record["phase"] = "Verified"
            self.save()
            return
        resources = self.inventory.resources()
        pods = self.kube.list("pods", self.state["daemonset_uid"])
        peers = {node["node_id"] for node in self.state["nodes"]
                 if not (node.get("register_source") and node["phase"] == "Pending")}
        peers.update(self.state.get("validated_capacity_nodes", {}))
        sources = [p for p in pods if p.get("spec", {}).get("nodeName") != identity
                   and p.get("spec", {}).get("nodeName") in peers
                   and resources.get(p.get("spec", {}).get("nodeName"), {}).get("status") == 0
                   and not p["metadata"].get("deletionTimestamp")]
        runtime = getattr(self.canary, "runtime", "runsc")
        def fits_canary(pod):
            resource = resources[pod["spec"]["nodeName"]]
            free = resource["allocatable"]
            classes = resource.get("runtime_classes", resource.get("runtimeClasses"))
            return (free["CPU"] >= self.cfg.canary_cpu and free["Memory"] >= self.cfg.canary_memory
                    and (classes is None or isinstance(classes, list) and runtime in classes))
        sources = [p for p in sources if fits_canary(p)]
        if not sources:
            raise Blocked("no healthy peer available for shared C/R canary")
        source = sources[0]
        grant = self.management.canary(self.state, identity, uid,
            source["spec"]["nodeName"], source["metadata"]["uid"], self.cfg.checkpoint_storage_alias,
            self.canary.namespace(), record["attempt"])
        if grant.get("ready") is not True:
            raise Blocked("node is not basically ready; scaler Completed is not required")
        record["grant"] = grant
        record["node_session_id"] = self.management.inspect(identity).get("node_session_id")
        record["phase"] = "Probing"
        self.save()
        self.canary.run(self.state, identity, grant)
        record["phase"] = "Probed"
        self.save()
        self.cleanup_canary(grant)
        proof = self.management.quiesce(self.state, identity)
        self.validate_empty(proof, uid)
        record["phase"] = "Verifying"
        self.save()
        self.management.verify(self.state, identity, uid, proof)
        record["phase"] = "Verified"
        self.save()

    def cleanup_canary(self, grant):
        identity = grant.get("environment_id")
        if not identity:
            raise Blocked("task canary cleanup requires its canonical identity")
        deadline = self.clock() + self.cfg.node_timeout_seconds
        while True:
            result = self.management.revoke_canary(self.state, identity)
            if (result.get("environment_id") != identity or result.get("revoked") is not True
                    or result.get("creation_fenced") is not True):
                raise UnknownOutcome("task canary creation fence is unconfirmed")
            if result.get("cleanup_confirmed") is True:
                return
            if self.clock() >= deadline:
                raise UnknownOutcome("task canary cleanup remains unconfirmed")
            self.sleep(self.cfg.poll_seconds)

    def cleanup_task_canaries(self):
        task = self.migration_task("Aborting")
        canaries = task.get("canaries", {})
        if not isinstance(canaries, dict):
            raise UnknownOutcome("authoritative task canary inventory is invalid")
        identities = set(canaries)
        identities.update(record["grant"]["environment_id"]
                          for record in self.state.get("validations", {}).values()
                          if record.get("grant"))
        for identity in sorted(identities):
            self.cleanup_canary({"environment_id": identity})
        # Freeze also revoked an authorization whose reply never reached this
        # journal. Start a fresh deterministic identity for that unfinished round.
        for record in self.state.get("validations", {}).values():
            if record["phase"] == "Authorizing":
                record["attempt"] += 1
        self.save()

    def migration_task(self, phase="Active"):
        task = self.management.status(self.state)
        expected = {key: self.state[key] for key in
                    ("task_id", "epoch", "namespace", "daemonset_uid")}
        expected.update(target_image=self.state["image"], phase=phase)
        if (any(task.get(key) != value for key, value in expected.items())
                or not isinstance(task.get("operations"), dict)):
            raise UnknownOutcome("authoritative migration task identity/phase differs")
        return task

    def migration_receipt(self, node, environment, operation, saved):
        if (not isinstance(saved, dict) or saved.get("environment_id") != environment
                or saved.get("source_node_id") != node["node_id"]
                or not saved.get("target_node_id") or saved["target_node_id"] == node["node_id"]
                or type(saved.get("source_generation")) is not int
                or saved.get("source_generation") != operation.get("source_generation")
                or type(saved.get("source_revision")) is not int
                or saved["source_revision"] < operation["source_revision"]
                or saved.get("source_session_id") != node["node_session_id"]
                or type(saved.get("was_running")) is not bool
                or (operation.get("target_node_id")
                    and operation["target_node_id"] != saved["target_node_id"])):
            raise UnknownOutcome("migration receipt identity/generation differs from durable intent")
        operation["target_node_id"] = saved["target_node_id"]
        operation["expected_state"] = "running" if saved["was_running"] else "paused"

    def check_migration_source(self, task, node):
        binding = task.get("nodes", {}).get(node["node_id"], {})
        phases = ("evacuating", "released", "RestoredOriginal") if task["phase"] == "Aborting" else ("evacuating",)
        if (binding.get("pod_uid") != node["old_uid"]
                or binding.get("node_session_id") != node["node_session_id"]
                or binding.get("phase") not in phases
                or self.management.inspect(node["node_id"]).get("node_session_id") != node["node_session_id"]):
            raise UnknownOutcome("unaccepted migration source Pod/session binding changed")

    def unaccepted_source(self, task, node, environment, operation):
        self.check_migration_source(task, node)
        current = next((item for item in self.inventory.instances()
                        if item["instanceID"] == environment), None)
        if (current is None or current["node_id"] != node["node_id"]
                or any(current.get(key) != operation.get("source_" + key)
                       for key in ("generation", "revision"))
                or any(current.get(key) for key in
                       ("invalidated", "restart_pending", "resume_pending", "recovery_pending"))
                or current["status"].lower() not in ("running", "paused")):
            raise UnknownOutcome("unaccepted migration source changed; same operation cannot be replayed")
        return current

    def reconcile_migration(self, node, environment, operation):
        task = self.migration_task()
        saved = task["operations"].get(operation["operation_id"])
        if saved is None:
            record = self.unaccepted_source(task, node, environment, operation)
            extra = {"expected_source_generation": operation["source_generation"],
                     "expected_source_revision": operation["source_revision"]}
        else:
            self.migration_receipt(node, environment, operation, saved)
            if saved.get("phase") == "Completed":
                # This is a durable historical receipt. A later user pause or
                # deletion does not undo acknowledged adoption and retirement.
                operation["completed"] = True
                self.save()
                return
            if saved.get("phase") not in ("Prepared", "Assigned"):
                raise UnknownOutcome("accepted migration is not in a resumable phase")
            record = {"instanceID": environment, "status": operation["expected_state"]}
            extra = {"target_node_id": saved["target_node_id"]}
        outcome = self.management.migrate(self.state, node["node_id"], record,
                                         operation["operation_id"], **extra)
        self.complete_migration(node, environment, operation, outcome)

    def complete_migration(self, node, environment, operation, outcome):
        if outcome.get("outcome") != "completed":
            raise UnknownOutcome("migration is pending/failed/unknown; inspect same operation")
        saved = self.migration_task()["operations"].get(operation["operation_id"])
        self.migration_receipt(node, environment, operation, saved)
        if saved.get("phase") != "Completed":
            raise UnknownOutcome("migration response has no matching Completed receipt")
        operation["completed"] = True
        self.save()

    def upgrade_node(self, node):
        identity = node["node_id"]
        if node["phase"] == "Done":
            return
        if node["phase"] == "Pending":
            self.capacity(node)
            if node.get("register_source"):
                self.check_node_identity(identity, node["node_uid"], node["pool_id"], node["instance_id"])
                pods = [p for p in self.kube.list("pods", self.state["daemonset_uid"])
                        if p.get("spec", {}).get("nodeName") == identity]
                if (len(pods) != 1 or pods[0]["metadata"]["uid"] != node["old_uid"]
                        or pods[0]["metadata"].get("deletionTimestamp")):
                    raise Conflict("late source Pod changed before registration")
            prepared = self.management.prepare(self.state, identity)
            node["node_session_id"] = prepared.get("node_session_id", node["node_session_id"])
            node["phase"] = "Migrating"
            self.save()
        if node["phase"] == "Migrating":
            # Source inventory can become empty after assignment CAS while the
            # target restore remains unknown. Resolve every durable migration
            # before treating an empty source as successfully evacuated.
            pending = node.setdefault("migrations", {})
            for environment, operation in pending.items():
                if operation["completed"]:
                    continue
                self.reconcile_migration(node, environment, operation)
            # Full inventory is refreshed before each individual migration.
            while True:
                self.inventory.resources()
                records = self.inventory.instances(identity)
                if not records:
                    break
                record = records[0]
                expected = "paused" if record["status"].lower() == "paused" else "running"
                if (type(record.get("generation")) is not int or record["generation"] <= 0
                        or type(record.get("revision")) is not int or record["revision"] < 0):
                    raise Blocked("migration requires authoritative source generation/revision")
                operation = pending.setdefault(record["instanceID"], {
                    "operation_id": op_id(self.cfg.task_id, identity, record["instanceID"]),
                    "expected_state": expected, "completed": False,
                    "source_generation": record["generation"], "source_revision": record["revision"]})
                self.save()
                outcome = self.management.migrate(self.state, identity, record,
                    operation["operation_id"], expected_source_generation=operation["source_generation"])
                self.complete_migration(node, record["instanceID"], operation, outcome)
            node["phase"] = "Quiescing"
            self.save()
        if node["phase"] == "Quiescing":
            proof = self.management.quiesce(self.state, identity)
            self.validate_empty(proof, node["old_uid"])
            node["proof"] = proof
            node["phase"] = "Deleting"
            self.save()
        if node["phase"] == "Deleting":
            pods = self.kube.list("pods", self.state["daemonset_uid"])
            old = next((p for p in pods if p["metadata"]["uid"] == node["old_uid"]), None)
            if old is not None:
                if old["metadata"].get("deletionTimestamp"):
                    raise UnknownOutcome("old Pod is already terminating; inspect without another delete")
                self.checked_ds()
                node["delete_intent"] = {"uid": old["metadata"]["uid"],
                                         "resourceVersion": old["metadata"]["resourceVersion"]}
                self.save()
                self.management.confirm_delete(self.state, identity, node["proof"], old)
                self.kube.delete_pod(old)
            node["phase"] = "Waiting"
            self.save()
        if node["phase"] == "Waiting":
            replacement = self.wait_replacement(node, self.cfg.image)
            node["new_uid"] = replacement["metadata"]["uid"]
            node["phase"] = "Canary"
            self.save()
        if node["phase"] == "Canary":
            # Basic node readiness precedes SDK tests; scaler completion may
            # depend on these tests and must not be awaited here.
            self.validate_node(identity, node["new_uid"])
            node["phase"] = "Done"
            self.save()

    @staticmethod
    def validate_empty(proof, uid):
        if (proof.get("pod_uid") != uid or not proof.get("node_session_id")
                or not proof.get("permit_id") or proof.get("strict_empty") is not True
                or str(proof.get("phase", "")).lower() != "quiescing"
                or type(proof.get("revision")) is not int):
            raise Blocked("strict empty proof and session-bound delete permit required")

    def wait_replacement(self, node, image, *, deleted_uid=None):
        deadline = self.clock() + self.cfg.node_timeout_seconds
        expected = image.split("@", 1)[1]
        while True:
            self.inventory.resources()
            self.inventory.instances()
            self.checked_ds()
            pods = [p for p in self.kube.list("pods", self.state["daemonset_uid"])
                    if p.get("spec", {}).get("nodeName") == node["node_id"]]
            if len(pods) == 1 and pods[0]["metadata"]["uid"] != (deleted_uid or node["old_uid"]):
                pod = pods[0]
                statuses = pod.get("status", {}).get("containerStatuses", [])
                status = next((s for s in statuses if s["name"] == self.cfg.container), {})
                if status.get("ready") is True and status.get("imageID", "").endswith(expected):
                    return pod
            if self.clock() >= deadline:
                raise Blocked("replacement Pod readiness/imageID not confirmed; isolation retained")
            self.sleep(self.cfg.poll_seconds)

    def abort(self):
        self.load()
        if self.reconcile_terminal():
            return self.state
        if self.state["phase"] == "Completed":
            raise Blocked("completed task is immutable")
        if self.state["phase"] == "Aborted":
            self.wait_scaler(release=True)
            return self.state
        self.wait_scaler()
        self.state["phase"] = "Aborting"
        self.save()
        self.freeze_baseline()
        observed = self.management.begin(self.state)
        if self.reconcile_terminal(observed):
            return self.state
        self.management.freeze(self.state)
        self.publish(self.state["old_image"])
        self.cleanup_task_canaries()
        # Management owns isolation and outstanding operations. An unavailable
        # Inspect never permits this runner to invent an empty-node proof.
        while True:
            for node in self.state["nodes"]:
                self.abort_node(node)
            if self.finish_scaling():
                break
        self.restore_bootstrap("Aborted", scaling_finalized=True)
        pods = self.kube.list("pods", self.state["daemonset_uid"])
        self.management.finish(self.state, "Aborted", pods=pods)
        self.state["phase"] = "Aborted"
        self.save()
        self.wait_scaler(release=True)
        return self.state

    def abort_node(self, node):
        if node["phase"] in ("Done", "RestoredOriginal"):
            return
        if self.resume_rollback(node):
            return
        self.revoke_pending_delete(node)
        pods = self.kube.list("pods", self.state["daemonset_uid"])
        matches = [p for p in pods if p.get("spec", {}).get("nodeName") == node["node_id"]]
        if len(matches) != 1 or matches[0]["metadata"].get("deletionTimestamp"):
            raise Blocked("rollback requires one current non-terminating node Pod")
        pod = matches[0]
        if pod["metadata"]["uid"] == node["old_uid"]:
            if node.get("register_source"):
                self.check_node_identity(node["node_id"], node["node_uid"], node["pool_id"], node["instance_id"])
            task = self.migration_task("Aborting")
            operations = task["operations"]
            for environment, operation in node.get("migrations", {}).items():
                observed = operations.get(operation["operation_id"])
                if observed is None:
                    # Freeze is serialized with management requests. Its
                    # acknowledged Aborting phase rejects a delayed forward
                    # request, so absence can be cancelled, never completed.
                    self.check_migration_source(task, node)
                    operation["cancelled"] = True
                    self.save()
                    continue
                self.migration_receipt(node, environment, operation, observed)
                if observed.get("phase") == "Cancelled":
                    operation["cancelled"] = True
                    self.save()
                    continue
                if observed.get("phase") != "Completed":
                    raise UnknownOutcome("original Pod cannot reopen before its migration outcomes are confirmed")
            if any(operation.get("phase") not in ("Completed", "Cancelled")
                   for operation in operations.values()):
                raise UnknownOutcome("accepted migration remains unconfirmed; original maintenance retained")
            result = self.management.restore_original(self.state, node["node_id"], pod)
            if (result.get("restored_original") is not True
                    or result.get("node_id") != node["node_id"]
                    or result.get("pod_uid") != node["old_uid"]
                    or result.get("node_session_id") != node["node_session_id"]):
                raise UnknownOutcome("original Pod maintenance restoration is not confirmed")
            node["phase"] = "RestoredOriginal"
            self.save()
        else:
            result = self.management.rollback(self.state, node["node_id"], pod)
            self.validate_empty(result, pod["metadata"]["uid"])
            pods = self.kube.list("pods", self.state["daemonset_uid"])
            matches = [p for p in pods if p.get("spec", {}).get("nodeName") == node["node_id"]]
            if len(matches) != 1 or matches[0]["metadata"]["uid"] != result.get("pod_uid"):
                raise Conflict("rollback Pod identity changed")
            pod = matches[0]
            node["rollback_delete_intent"] = {"uid": pod["metadata"]["uid"],
                                              "resourceVersion": pod["metadata"]["resourceVersion"]}
            node["rollback_phase"] = "Deleting"
            self.save()
            self.management.confirm_delete(self.state, node["node_id"], result, pod)
            self.kube.delete_pod(pod)
            node["rollback_phase"] = "Waiting"
            self.save()
            self.resume_rollback(node)

    def resume_rollback(self, node):
        phase = node.get("rollback_phase")
        if phase not in ("Deleting", "Waiting", "Canary"):
            return False
        intent = node["rollback_delete_intent"]
        if phase == "Deleting":
            old = next((p for p in self.kube.list("pods", self.state["daemonset_uid"])
                        if p["metadata"]["uid"] == intent["uid"]), None)
            if old is not None:
                # Revoke a possibly delayed DELETE before requesting any new
                # permit. Never transfer its UID/RV authority to a new Pod.
                self.revoke_pending_delete(node)
                node.pop("rollback_phase")
                self.save()
                return False
            node["rollback_phase"] = "Waiting"
            self.save()
        restored = self.wait_replacement(node, self.state["old_image"], deleted_uid=intent["uid"])
        uid = restored["metadata"]["uid"]
        if node.get("rollback_phase") == "Canary" and node["rollback_new_uid"] != uid:
            raise Conflict("rollback replacement Pod changed during validation")
        node["rollback_new_uid"] = uid
        node["rollback_phase"] = "Canary"
        self.save()
        self.validate_node(node["node_id"], uid)
        node["new_uid"] = uid
        node["phase"] = "Done"
        self.save()
        return True

    def revoke_pending_delete(self, node):
        intent = node.get("rollback_delete_intent") or node.get("delete_intent")
        if not intent:
            return
        pod = next((p for p in self.kube.list("pods", self.state["daemonset_uid"])
                    if p["metadata"]["uid"] == intent["uid"]), None)
        if pod is None:
            return  # A new UID can never be affected by the old UID precondition.
        if pod["metadata"].get("deletionTimestamp"):
            raise Blocked("delete intent is already Terminating; never reopen this Pod")
        annotations = dict(pod["metadata"].get("annotations", {}))
        annotations["akernel.io/upgrade-delete-revoked"] = op_id(
            self.cfg.task_id, node["node_id"], intent["resourceVersion"])
        fresh = self.kube.patch("pods", pod, [{"op": "add", "path": "/metadata/annotations", "value": annotations}])
        if fresh["metadata"]["resourceVersion"] == intent["resourceVersion"]:
            raise Conflict("Pod RV did not advance while revoking old DELETE")
        self.management.revoke_delete(self.state, node["node_id"], fresh, intent["resourceVersion"])
        node.pop("delete_intent", None)
        node.pop("rollback_delete_intent", None)
        self.save()
