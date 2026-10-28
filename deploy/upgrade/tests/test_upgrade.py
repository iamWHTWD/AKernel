"""Failure-oriented tests; no Kubernetes/cloud/ADX mutation is performed."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from akernel_upgrade.clients import HTTP, Inventory, Kubernetes
from akernel_upgrade.canary import SDKCanary
from akernel_upgrade.engine import Executor, Settings
from akernel_upgrade.errors import Blocked, Conflict, UnknownOutcome, UpgradeError
from akernel_upgrade.protocol import Management, Scaling

OLD = "repo/node@sha256:" + "1" * 64
NEW = "repo/node@sha256:" + "2" * 64


class FakeKube:
    def __init__(self):
        self.events = []
        self.maps = {}
        self.ds = {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {
            "name": "node", "uid": "ds", "resourceVersion": "1", "generation": 1},
            "spec": {"updateStrategy": {"type": "RollingUpdate"}, "template": {"spec": {
                "containers": [{"name": "akernel-node", "image": OLD}]}}},
            "status": {"observedGeneration": 1}}
        self.pods = [self.pod("a", "a-old"), self.pod("b", "b-old")]
        self.fail_patch = False

    def pod(self, node, uid):
        image = self.ds["spec"]["template"]["spec"]["containers"][0]["image"]
        return {"metadata": {"name": uid, "uid": uid, "resourceVersion": "1",
                              "ownerReferences": [{"uid": "ds"}]},
                "spec": {"nodeName": node}, "status": {"containerStatuses": [{
                    "name": "akernel-node", "ready": True, "imageID": image}]}}

    def get(self, kind, name):
        return copy.deepcopy(self.ds if kind == "daemonsets" else self.maps[name])

    def get_node(self, name):
        return {"metadata": {"name": name, "uid": "node-" + name,
                             "labels": {"alibabacloud.com/nodepool-id": "pool"}},
                "spec": {"providerID": "cn-test.i-" + name}}

    def list(self, kind, owner=None):
        return copy.deepcopy(self.pods)

    def patch(self, kind, obj, operations):
        if self.fail_patch:
            raise UnknownOutcome("lost patch response")
        if kind == "daemonsets":
            target = self.ds
        elif kind == "pods":
            target = next(p for p in self.pods if p["metadata"]["uid"] == obj["metadata"]["uid"])
        else:
            target = self.maps[obj["metadata"]["name"]]
        if obj["metadata"]["resourceVersion"] != target["metadata"]["resourceVersion"]:
            raise Conflict("stale state")
        for op in operations:
            parts = op["path"].lstrip("/").split("/")
            cursor = target
            for part in parts[:-1]:
                cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
            key = int(parts[-1]) if isinstance(cursor, list) else parts[-1]
            cursor[key] = copy.deepcopy(op["value"])
            self.events.append((kind, op["path"], copy.deepcopy(op["value"])))
        target["metadata"]["resourceVersion"] = str(int(target["metadata"]["resourceVersion"]) + 1)
        if kind == "daemonsets":
            target["metadata"]["generation"] += 1
            target["status"]["observedGeneration"] = target["metadata"]["generation"]
        return copy.deepcopy(target)

    def create_configmap(self, name, data):
        if name in self.maps:
            raise Conflict("active task exists")
        value = {"metadata": {"name": name, "uid": name, "resourceVersion": "1"}, "data": data}
        self.maps[name] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def save_state(self, obj, state):
        return self.patch("configmaps", obj, [{"op": "replace", "path": "/data/state.json",
                                               "value": json.dumps(state)}])

    def delete_pod(self, obj):
        self.events.append(("delete", obj["metadata"]["uid"]))
        self.pods = [p for p in self.pods if p["metadata"]["uid"] != obj["metadata"]["uid"]]
        self.pods.append(self.pod(obj["spec"]["nodeName"], obj["metadata"]["uid"] + "-new"))


class ControllerObservedKube(FakeKube):
    """The controller observes spec changes in a separate status/RV write."""

    def __init__(self):
        super().__init__()
        self.observation_reads = 0
        self.before_observation = None

    def patch(self, kind, obj, operations):
        observed = self.ds["status"]["observedGeneration"]
        result = super().patch(kind, obj, operations)
        if kind == "daemonsets":
            self.ds["status"]["observedGeneration"] = observed
            result["status"]["observedGeneration"] = observed
            self.observation_reads = 2
        return result

    def get(self, kind, name):
        if kind == "daemonsets" and self.observation_reads:
            self.observation_reads -= 1
            if not self.observation_reads:
                if self.before_observation:
                    self.before_observation(self.ds)
                self.ds["status"]["observedGeneration"] = self.ds["metadata"]["generation"]
                self.ds["metadata"]["resourceVersion"] = str(
                    int(self.ds["metadata"]["resourceVersion"]) + 1)
                self.events.append(("observed", self.ds["metadata"]["generation"]))
        return super().get(kind, name)


class FakeInventory:
    def __init__(self):
        self.records = [{"instanceID": "work", "node_id": "a", "status": "running",
                         "generation": 1, "revision": 1}]
        self.available = True
        self.reads = 0

    def resources(self):
        self.reads += 1
        return {node: {"status": 0, "capacity": {"CPU": 10000, "Memory": 10000},
                       "allocatable": {"CPU": 9000 if self.available else 0,
                                       "Memory": 9000 if self.available else 0}}
                for node in ("a", "b")}

    def instances(self, node=None):
        return [r.copy() for r in self.records if node is None or r["node_id"] == node]


class FakeManagement:
    def __init__(self, kube, inventory):
        self.kube, self.inventory = kube, inventory
        self.events = []
        self.migration_outcome = "completed"
        self.proof_empty = True
        self.verified = {}
        self.task = None

    def capabilities(self):
        return {"version": 1, **{name: True for name in Executor.REQUIRED}}

    def inspect(self, node):
        return self.verified.get(node, {"node_session_id": "session-" + node})

    def begin(self, state):
        self.events.append("begin")
        if self.task is None:
            self.task = {key: state[key] for key in ("task_id", "epoch", "namespace", "daemonset_uid")}
            self.task.update(target_image=state["image"], phase="Active", operations={}, nodes={
                n["node_id"]: {"pod_uid": n["old_uid"], "node_session_id": n["node_session_id"],
                               "phase": "pending"} for n in state["nodes"]})

    def status(self, state):
        return copy.deepcopy(self.task)

    def prepare(self, state, node):
        self.events.append("prepare-" + node)
        if node not in self.task["nodes"]:
            source = next(n for n in state["nodes"] if n["node_id"] == node)
            if source.get("register_source") is not True:
                raise Conflict("unknown migration source")
            self.task["nodes"][node] = {"pod_uid": source["old_uid"],
                "node_session_id": source["node_session_id"], "phase": "pending"}
        self.task["nodes"][node]["phase"] = "evacuating"
        return self.inspect(node)

    def migrate(self, state, node, record, operation_id, **extra):
        self.events.append("migrate")
        target = "b" if node == "a" else "a"
        if operation_id not in self.task["operations"]:
            self.task["operations"][operation_id] = {
                "environment_id": record["instanceID"], "source_node_id": node,
                "target_node_id": target, "source_generation": record["generation"],
                "source_revision": record["revision"], "source_session_id": "session-" + node,
                "target_session_id": "session-" + target,
                "was_running": record["status"] != "paused", "phase": "Prepared"}
        if self.migration_outcome == "unknown":
            raise UnknownOutcome("reply lost")
        self.task["operations"][operation_id]["phase"] = "Completed"
        self.inventory.records = [r for r in self.inventory.records if r["instanceID"] != record["instanceID"]]
        return {"outcome": self.migration_outcome, "node_id": target, "state": record["status"]}

    def quiesce(self, state, node):
        self.events.append("quiesce")
        pod = next(p for p in self.kube.pods if p["spec"]["nodeName"] == node)
        return {"strict_empty": self.proof_empty, "pod_uid": pod["metadata"]["uid"],
                "node_session_id": "session", "permit_id": "permit", "phase": "Quiescing", "revision": 1}

    def confirm_delete(self, *args):
        self.events.append("confirm-delete")

    def canary(self, *args):
        return {"ready": True, "environment_id": "test"}

    def revoke_canary(self, state, environment_id):
        self.events.append("revoke-canary-" + environment_id)
        return {"environment_id": environment_id, "revoked": True,
                "creation_fenced": True, "cleanup_confirmed": True}

    def verify(self, *args):
        self.events.append("verify")
        state, node, uid, _ = args
        self.verified[node] = {"node_session_id": "session-" + node, "pod_uid": uid,
                               "task_id": state["task_id"], "epoch": state["epoch"], "phase": "verified"}

    def finish(self, state, phase, **extra):
        self.events.append(phase)

    def freeze(self, state):
        self.events.append("freeze")
        self.task["phase"] = "Aborting"

    def revoke_delete(self, state, node, pod, expected_delete_resource_version):
        self.events.append("revoke-delete-" + pod["metadata"]["resourceVersion"])


class FakeCanary:
    def __init__(self):
        self.error = None
        self.runs = 0

    def preflight(self):
        return []

    def namespace(self):
        return "default"

    def run(self, *args):
        self.runs += 1
        if self.error:
            raise self.error


class FakeScaling:
    def __init__(self):
        self.protected = False
        self.requests = 0
        self.requested_nodes = set()

    def configuration(self):
        return {"name": "guard", "cluster_id": "cluster", "pool_id": "pool", "count": 1}

    def protect(self, state):
        self.protected = True

    def release(self, state):
        self.protected = False

    def ready_nodes(self, state, *, include_pending=False):
        return []

    def finalize(self, state):
        pass

    def request_capacity(self, state, node, *args):
        if node["node_id"] not in self.requested_nodes:
            self.requested_nodes.add(node["node_id"])
            self.requests += 1


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.kube, self.inventory = FakeKube(), FakeInventory()
        self.manager = FakeManagement(self.kube, self.inventory)
        self.probe, self.scaling = FakeCanary(), FakeScaling()
        self.now = 0
        self.settings = Settings("test", "node", NEW, "task", "executor",
                                 checkpoint_storage_alias="shared")
        self.engine = Executor(self.settings, self.kube, self.inventory, self.manager,
                               self.probe, self.scaling, clock=lambda: self.now, sleep=self.tick)

    def tick(self, value):
        self.now += value

    def test_plan_does_not_mutate(self):
        self.assertTrue(self.engine.plan()["read_only"])
        self.assertEqual(self.kube.events, [])
        self.assertEqual(self.manager.events, [])

    def test_order_and_full_completion(self):
        self.assertEqual(self.engine.apply()["phase"], "Completed")
        ds_events = [e for e in self.kube.events if e[0] == "daemonsets"]
        self.assertEqual(ds_events[0][1], "/spec/updateStrategy")
        self.assertTrue(ds_events[1][1].endswith("/env"))
        self.assertTrue(ds_events[2][1].endswith("/image"))
        self.assertEqual(self.probe.runs, 2)
        env = self.kube.ds["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertNotIn("ADX_NODE_UPGRADE_ISOLATED", [item["name"] for item in env])
        self.assertFalse(self.scaling.protected)
        self.assertLess(self.manager.events.index("quiesce"), self.manager.events.index("confirm-delete"))

    def test_unknown_migration_does_not_delete_or_release(self):
        self.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.assertFalse(any(e[0] == "delete" for e in self.kube.events))
        self.assertTrue(self.scaling.protected)
        self.assertEqual(self.engine.state["phase"], "NeedsAttention")

    def test_empty_source_after_lost_migration_reply_is_not_evacuated(self):
        self.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.inventory.records = []  # Assignment moved, target result is still unknown.
        with self.assertRaises(UnknownOutcome):
            self.engine.run()
        self.assertFalse(any(e[0] == "delete" for e in self.kube.events))

    def test_pending_delete_is_revoked_only_after_atomic_pod_rv_bump(self):
        self.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        node = self.engine.state["nodes"][0]
        node["delete_intent"] = {"uid": "a-old", "resourceVersion": "1"}
        self.engine.revoke_pending_delete(node)
        self.assertIn("revoke-delete-2", self.manager.events)
        self.assertNotIn("delete_intent", node)

    def test_terminal_bootstrap_cleanup_refuses_unverified_owner_pod(self):
        self.engine.apply()
        self.kube.pods.append(self.kube.pod("unknown", "unexpected-pod"))
        with self.assertRaises(Blocked):
            self.engine.restore_bootstrap("Completed")

    def test_abort_freezes_persistent_task_before_unknown_rpc(self):
        self.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        def unknown(state): raise UnknownOutcome("freeze reply lost")
        self.manager.freeze = unknown
        with self.assertRaises(UnknownOutcome):
            self.engine.abort()
        self.assertEqual(self.engine.state["phase"], "Aborting")
        with self.assertRaises(Blocked):
            self.engine.run()
        self.assertTrue(self.scaling.protected)

    def test_published_failed_is_not_success(self):
        self.manager.migration_outcome = "failed"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.assertFalse(any(e[0] == "delete" for e in self.kube.events))

    def test_missing_authoritative_canary_cleanup_blocks_before_any_mutation(self):
        supported = self.manager.capabilities()
        supported.pop("canary_cleanup")
        self.manager.capabilities = lambda: supported
        with self.assertRaises(Blocked):
            self.engine.apply()
        self.assertFalse(self.kube.events)
        self.assertFalse(self.kube.maps)
        self.assertFalse(self.manager.events)

    def test_nonempty_proof_does_not_delete(self):
        self.manager.proof_empty = False
        with self.assertRaises(Blocked):
            self.engine.apply()
        self.assertFalse(any(e[0] == "delete" for e in self.kube.events))

    def test_unknown_canary_does_not_open_or_delete_replacement(self):
        self.probe.error = UnknownOutcome("SDK reply lost")
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.assertNotIn("verify", self.manager.events)
        self.assertEqual(len([e for e in self.kube.events if e[0] == "delete"]), 1)
        self.assertTrue(self.scaling.protected)
        env = self.kube.ds["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual(env[-1], {"name": "ADX_NODE_UPGRADE_ISOLATED", "value": "true"})

    def test_expansion_canary_does_not_wait_for_scaler_completed(self):
        self.engine.apply()
        self.kube.pods.append(self.kube.pod("d", "pod-d"))
        self.scaling.ready_nodes = lambda state, **kwargs: [{"name": "d", "uid": "node-d",
            "pool_id": "pool", "instance_id": "i-d", "ready_for_validation": True}]
        before = self.probe.runs
        self.engine.validate_capacity_nodes()
        self.assertEqual(self.probe.runs, before + 1)
        self.engine.validate_capacity_nodes()
        self.assertEqual(self.probe.runs, before + 1)

    def test_successful_probe_is_not_repeated_after_quiesce_timeout(self):
        original = self.manager.quiesce
        calls = 0
        def flaky(state, node):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise UnknownOutcome("quiesce response lost after completed probe")
            return original(state, node)
        self.manager.quiesce = flaky
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.assertEqual(self.probe.runs, 1)
        self.assertEqual(self.engine.state["validations"]["a"]["phase"], "Probed")
        self.engine.run()
        self.assertEqual(self.probe.runs, 2)  # One for A, one for B.

    def test_verified_gate_reconciles_lost_verify_reply_without_reprobing(self):
        original = self.manager.verify
        failed = False
        def flaky(*args):
            nonlocal failed
            original(*args)
            if not failed:
                failed = True
                raise UnknownOutcome("verify response lost")
        self.manager.verify = flaky
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.assertEqual(self.engine.state["validations"]["a"]["phase"], "Verifying")
        self.engine.run()
        self.assertEqual(self.probe.runs, 2)

    def test_capacity_waits_sixty_seconds_and_persists_request(self):
        self.inventory.available = False
        with self.assertRaises(Blocked):
            self.engine.apply()
        self.assertGreaterEqual(self.now, 60)
        self.assertEqual(self.scaling.requests, 1)
        self.assertNotIn("prepare-a", self.manager.events)

    def test_later_source_need_includes_workload_received_since_apply(self):
        self.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        original = self.inventory.resources
        def changed():
            value = original()
            value["b"]["capacity"] = {"CPU": 20000, "Memory": 20000}
            value["b"]["allocatable"] = {"CPU": 1000, "Memory": 1000}
            return value
        self.inventory.resources = changed
        node = self.engine.state["nodes"][1]
        self.assertEqual(node["need"]["CPU"], 1000)
        with self.assertRaises(Blocked):
            self.engine.capacity(node)
        self.assertEqual(node["need"]["CPU"], 19000)
        self.assertEqual(self.scaling.requests, 1)
        self.assertNotIn("prepare-b", self.manager.events)

    def test_canary_skips_full_peer_and_uses_peer_that_fits(self):
        self.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.kube.pods.append(self.kube.pod("c", "c-pod"))
        self.engine.state["validated_capacity_nodes"] = {"c": {"node_uid": "node-c", "pod_uid": "c-pod"}}
        original = self.inventory.resources
        def resources():
            value = original()
            value["b"]["allocatable"] = {"CPU": 0, "Memory": 0}
            value["c"] = {"status": 0, "capacity": {"CPU": 4000, "Memory": 4000},
                          "allocatable": {"CPU": 4000, "Memory": 4000}, "runtime_classes": ["runsc"]}
            return value
        self.inventory.resources = resources
        sources = []
        def authorize(*args):
            sources.append(args[3])
            return {"ready": True, "environment_id": "canary"}
        self.manager.canary = authorize
        self.engine.validate_node("a", "a-old")
        self.assertEqual(sources, ["c"])

    def test_second_task_and_executor_are_fenced(self):
        self.engine.apply()
        with self.assertRaises(Conflict):
            self.engine.apply()
        changed = Settings("test", "node", NEW, "task", "other", checkpoint_storage_alias="shared")
        other = Executor(changed, self.kube, self.inventory, self.manager, self.probe, self.scaling)
        with self.assertRaises(Conflict):
            other.load()

    def test_config_drift_blocks_patch(self):
        self.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            self.engine.apply()
        self.kube.ds["spec"]["template"]["spec"]["hostNetwork"] = True
        with self.assertRaises(Conflict):
            self.engine.publish(NEW)

    def test_wrong_uid_or_unconfirmed_permit_is_rejected(self):
        proof = {"strict_empty": True, "pod_uid": "stale", "node_session_id": "s",
                 "permit_id": "p", "phase": "Quiescing", "revision": 1}
        with self.assertRaises(Blocked):
            self.engine.validate_empty(proof, "current")


class PublishObservationTests(unittest.TestCase):
    def fixture(self, strategy="OnDelete"):
        case = EngineTests()
        case.setUp()
        case.kube = ControllerObservedKube()
        case.kube.ds["spec"]["updateStrategy"] = {"type": strategy}
        case.engine.kube = case.manager.kube = case.kube
        return case

    def assert_publish_after_controller_observation(self, strategy):
        case = self.fixture(strategy)
        self.assertEqual(case.engine.apply()["phase"], "Completed")
        events = case.kube.events
        marker = next(i for i, event in enumerate(events) if event[0] == "daemonsets"
                      and event[1].endswith("/env"))
        images = [i for i, event in enumerate(events) if event[0] == "daemonsets"
                  and event[1].endswith("/image")]
        self.assertEqual(len(images), 1)
        marker_generation = 2 if strategy == "OnDelete" else 3
        self.assertLess(marker, events.index(("observed", marker_generation)))
        self.assertLess(events.index(("observed", marker_generation)), images[0])
        self.assertFalse(any(event[0] == "delete" for event in events[:images[0]]))
        if strategy == "RollingUpdate":
            self.assertLess(events.index(("observed", 2)), marker)

    def test_on_delete_image_patch_uses_observed_marker_resource_version(self):
        self.assert_publish_after_controller_observation("OnDelete")

    def test_rolling_update_marker_patch_uses_observed_strategy_resource_version(self):
        self.assert_publish_after_controller_observation("RollingUpdate")

    def test_controller_observation_still_rejects_identity_and_configuration_drift(self):
        for change in ("uid", "generation", "template"):
            with self.subTest(change=change):
                case = self.fixture()
                def drift(ds):
                    if change == "uid":
                        ds["metadata"]["uid"] = "replacement"
                    elif change == "generation":
                        ds["metadata"]["generation"] += 1
                    else:
                        ds["spec"]["template"]["spec"]["hostNetwork"] = True
                case.kube.before_observation = drift
                with self.assertRaises(Conflict):
                    case.engine.apply()
                self.assertEqual(case.engine.state["phase"], "NeedsAttention")
                self.assertEqual(case.kube.ds["spec"]["template"]["spec"]["containers"][0]["image"], OLD)
                self.assertFalse(any(event[0] == "delete" for event in case.kube.events))

    def test_status_write_after_observation_still_conflicts_without_retry(self):
        case = self.fixture()
        save = case.engine.save
        def status_write():
            save()
            if case.engine.state.get("template_intent") == NEW:
                case.kube.ds["metadata"]["resourceVersion"] = str(
                    int(case.kube.ds["metadata"]["resourceVersion"]) + 1)
        case.engine.save = status_write
        with self.assertRaises(Conflict):
            case.engine.apply()
        self.assertEqual(case.kube.ds["spec"]["template"]["spec"]["containers"][0]["image"], OLD)
        self.assertFalse(any(event[0] == "delete" for event in case.kube.events))


class TransportTests(unittest.TestCase):
    def test_capacity_node_read_uses_cluster_scoped_endpoint(self):
        kube = Kubernetes.__new__(Kubernetes)
        calls = []
        kube.call = lambda method, path: calls.append((method, path)) or {}
        kube.get_node("worker-a")
        self.assertEqual(calls, [("GET", "/api/v1/nodes/worker-a")])

    def test_late_source_registration_keeps_the_persisted_source_session(self):
        calls = []
        class HTTP:
            def request(self, method, path, body=None):
                if method == "GET":
                    return {"node_session_id": "a-new-session"}
                calls.append(body)
                return {}
        state = {"task_id": "t", "epoch": 1, "nodes": [
            {"node_id": "d", "old_uid": "old-d", "node_session_id": "saved-session",
             "register_source": True}]}
        management = Management(HTTP())
        management.prepare(state, "d")
        self.assertTrue(calls[0]["register_source"])
        self.assertEqual(calls[0]["pod_uid"], "old-d")
        self.assertEqual(calls[0]["node_session_id"], "saved-session")
        state["phase"] = "Aborting"
        management.restore_original(state, "d", {"metadata": {"uid": "old-d", "resourceVersion": "5"}})
        self.assertTrue(calls[-1]["register_source"])
        self.assertEqual(calls[-1]["node_session_id"], "saved-session")
        self.assertEqual(calls[-1]["stage"], "restore-original")

    def test_kubernetes_patch_and_delete_have_identity_preconditions(self):
        kube = Kubernetes.__new__(Kubernetes)
        kube.namespace = "ns"
        calls = []
        kube.call = lambda *a, **kw: calls.append((a, kw))
        obj = {"metadata": {"name": "node", "uid": "u", "resourceVersion": "7"}}
        kube.patch("daemonsets", obj, [{"op": "replace", "path": "/spec/x", "value": 1}])
        guards = calls[-1][0][2][:2]
        self.assertEqual([g["value"] for g in guards], ["u", "7"])
        kube.delete_pod(obj)
        self.assertEqual(calls[-1][0][2]["preconditions"], {"uid": "u", "resourceVersion": "7"})

    def test_public_token_never_reaches_management(self):
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token"
            token.write_text("private-value")
            http = HTTP("http://internal:19092", "https://public", str(token))
            seen = []
            class Reply:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def read(self, *a): return b'{}'
            http.opener.open = lambda request, **kw: (seen.append(request) or Reply())
            http.request("GET", "/internal")
            http.request("GET", "/resources", public=True)
            self.assertIsNone(seen[0].get_header("Authorization"))
            self.assertEqual(seen[1].get_header("Authorization"), "Bearer private-value")

    def test_permission_denial_is_rejection_but_server_failure_is_unknown(self):
        http = HTTP("http://internal", "https://public", "/unused")
        for status, expected in ((403, UpgradeError), (409, Conflict), (503, UnknownOutcome)):
            def fail(*args, **kwargs):
                raise urllib.error.HTTPError("http://internal", status, "bad", {}, None)
            http.opener.open = fail
            with self.assertRaises(expected):
                http.request("POST", "/mutation", {})

    def test_long_lifecycle_mutation_has_a_separate_budget_from_read_only_queries(self):
        http = HTTP("http://internal", "https://public", "/unused")
        timeouts = []
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return b'{}'
        def open_request(request, **kwargs):
            timeouts.append(kwargs["timeout"])
            return Reply()
        http.opener.open = open_request
        http.request("GET", "/status")
        http.request("POST", "/migrate", {})
        self.assertEqual(timeouts, [30, 660])

    def test_all_instance_pages_and_revision_conflict(self):
        class Pages:
            def __init__(self): self.revision = 1; self.calls = 0
            def request(self, method, path):
                self.calls += 1
                return {"count": 2, "epoch": 1, "revision": self.revision,
                        "instanceInfos": [{"instanceID": str(self.calls), "node_id": "a", "status": "paused"}]}
        pages = Pages()
        self.assertEqual(len(Inventory(pages, 1).instances()), 2)
        class Changing(Pages):
            def request(self, *args):
                value = super().request(*args)
                value["revision"] = self.calls
                return value
        with self.assertRaises(Conflict):
            Inventory(Changing(), 1).instances()

    def test_scaler_write_preserves_status_and_capacity_request_is_idempotent(self):
        kube = FakeKube()
        state = {"task_id": "t", "epoch": 1}
        status = {"version": 1, "cluster_id": "c", "observed_task_id": "t",
                  "observed_epoch": 1, "handoff": "Ready"}
        kube.create_configmap("guard", {"status.json": json.dumps(status)})
        scaler = Scaling(kube, "guard", "c", "p")
        scaler.protect(state)
        scaler.request_capacity(state, {"node_id": "a"}, {}, {})
        scaler.request_capacity(state, {"node_id": "a"}, {}, {})
        obj = kube.get("configmaps", "guard")
        self.assertEqual(json.loads(obj["data"]["status.json"]), status)
        self.assertEqual(len(json.loads(obj["data"]["request.json"])["capacity_requests"]), 1)
        with self.assertRaises(Blocked):
            scaler.release(state)
        self.assertEqual(json.loads(kube.get("configmaps", "guard")["data"]["request.json"])["phase"], "Released")
        status["handoff"] = "Released"
        kube.maps["guard"]["data"]["status.json"] = json.dumps(status)
        scaler.release(state)

    def test_stale_scaler_status_never_opens_guard(self):
        kube = FakeKube()
        kube.create_configmap("guard", {"status.json": json.dumps({"version": 1, "cluster_id": "c",
            "observed_task_id": "old", "observed_epoch": 1, "handoff": "Ready"})})
        with self.assertRaises(Blocked):
            Scaling(kube, "guard", "c", "p").protect({"task_id": "new", "epoch": 1})

    def test_management_canonical_canary_and_migration_contract(self):
        state = {"task_id": "t", "epoch": 1, "namespace": "test", "daemonset_uid": "ds",
                 "image": NEW, "nodes": []}
        task = {key: state[key] for key in ("task_id", "epoch", "namespace", "daemonset_uid")}
        task.update(target_image=NEW, phase="Active", nodes={"a": {"control_revision": 7}})
        class FakeHTTP:
            def __init__(self): self.calls = []
            def request(self, method, path, body=None):
                self.calls.append((method, path, body))
                if method == "GET" and "/tasks/" in path:
                    return copy.deepcopy(task)
                if method == "GET": return {"node_session_id": "current-session", "revision": 99}
                if path.endswith("authorize-canary"):
                    return {"environment_id": "default-" + body["environment_id"],
                            "name": body["environment_id"], "ready": True}
                return {"completed": True, "durability": "Published", "node_id": "a", "state": "Running"}
        http = FakeHTTP()
        management = Management(http)
        grant = management.canary(state, "a", "pod-a", "b", "pod-b", "shared", "default")
        payload = http.calls[-1][2]
        self.assertEqual(payload["node_id"], "b")
        self.assertEqual(payload["target_node_id"], "a")
        self.assertEqual(payload["environment_namespace"], "default")
        self.assertEqual(payload["expected_target_control_revision"], 7)
        self.assertEqual(grant["environment_id"], "default-" + grant["name"])
        outcome = management.migrate(state, "b", {"instanceID": grant["environment_id"]}, "same-op", target_node_id="a")
        self.assertEqual(outcome["outcome"], "completed")
        self.assertEqual(http.calls[-1][2]["operation_id"], "same-op")
        task["nodes"].clear()
        management.canary(state, "d", "pod-d", "b", "pod-b", "shared", "default")
        self.assertEqual(http.calls[-1][2]["expected_target_control_revision"], 0)

    def test_canary_authorization_rejects_task_identity_drift_before_post(self):
        state = {"task_id": "t", "epoch": 1, "namespace": "test", "daemonset_uid": "ds",
                 "image": NEW, "nodes": []}
        for key, value in (("task_id", "other"), ("epoch", 2), ("namespace", "other"),
                           ("daemonset_uid", "other"), ("target_image", OLD)):
            task = {key: state[key] for key in ("task_id", "epoch", "namespace", "daemonset_uid")}
            task.update(target_image=NEW, phase="Active", nodes={})
            task[key] = value
            calls = []
            class HTTP:
                def request(self, method, path, body=None):
                    calls.append(method)
                    return copy.deepcopy(task) if "/tasks/" in path else {"node_session_id": "session"}
            with self.subTest(field=key), self.assertRaises(Conflict):
                Management(HTTP()).canary(state, "a", "pod-a", "b", "pod-b", "shared", "default")
            self.assertNotIn("POST", calls)


class PhaseRecoveryTests(unittest.TestCase):
    def fixture(self):
        case = EngineTests()
        case.setUp()
        return case

    @staticmethod
    def terminal_manager(case, lost_phase=None):
        lost = False
        def finish(state, phase, **extra):
            nonlocal lost
            case.manager.task["phase"] = phase
            case.manager.verified = {node: {
                "task_id": state["task_id"], "epoch": state["epoch"],
                "pod_uid": "", "node_session_id": "session-" + node, "phase": "released",
            } for node in ("a", "b")}
            if phase == lost_phase and not lost:
                lost = True
                raise UnknownOutcome("terminal persisted; reply lost")
        case.manager.finish = finish

    @staticmethod
    def resumed(case):
        return Executor(case.settings, case.kube, case.inventory, case.manager,
                        case.probe, case.scaling, clock=lambda: case.now, sleep=case.tick)

    def test_completed_response_loss_never_republishes_bootstrap(self):
        case = self.fixture()
        self.terminal_manager(case, "Completed")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        # A later normal recreation is outside the completed upgrade's authority.
        case.kube.pods[0]["metadata"]["uid"] += "-restart"
        before = len(case.kube.events)
        self.assertEqual(self.resumed(case).run()["phase"], "Completed")
        self.assertFalse(any(e[0] == "daemonsets" for e in case.kube.events[before:]))
        self.assertFalse(case.scaling.protected)

    def test_completed_local_save_loss_uses_authoritative_terminal_receipt(self):
        case = self.fixture()
        self.terminal_manager(case)
        save = case.kube.save_state
        def lost(obj, state):
            if state["phase"] == "Completed":
                raise SystemExit("process stopped before local terminal save")
            return save(obj, state)
        case.kube.save_state = lost
        with self.assertRaises(SystemExit):
            case.engine.apply()
        case.kube.save_state = save
        before = len(case.kube.events)
        self.assertEqual(self.resumed(case).run()["phase"], "Completed")
        self.assertFalse(any(e[0] == "daemonsets" for e in case.kube.events[before:]))

    def test_aborted_response_loss_only_releases_scaler(self):
        case = self.fixture()
        case.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        case.manager.task["operations"].clear()
        case.manager.restore_original = lambda state, node, pod: {
            "restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
            "node_session_id": "session-" + node,
        }
        self.terminal_manager(case, "Aborted")
        with self.assertRaises(UnknownOutcome):
            case.engine.abort()
        before = len(case.kube.events), len(case.manager.events)
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertFalse(any(e[0] == "daemonsets" for e in case.kube.events[before[0]:]))
        self.assertEqual(case.manager.events[before[1]:], [])

    def test_abort_after_initial_scaler_handoff_failure_creates_then_freezes_same_task(self):
        case = self.fixture()
        protect = case.scaling.protect
        def unavailable(state):
            raise Blocked("scaler has not acknowledged the handoff")
        case.scaling.protect = unavailable
        with self.assertRaises(Blocked):
            case.engine.apply()
        self.assertIsNone(case.manager.task)
        self.assertEqual(case.engine.state["phase"], "NeedsAttention")
        case.scaling.protect = protect
        freeze = case.manager.freeze
        def freeze_existing(state):
            if case.manager.task is None:
                raise UpgradeError("HTTP request rejected (404)")
            return freeze(state)
        case.manager.freeze = freeze_existing
        case.manager.restore_original = lambda state, node, pod: {
            "restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
            "node_session_id": "session-" + node,
        }
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual(case.manager.events.count("begin"), 1)
        self.assertLess(case.manager.events.index("begin"), case.manager.events.index("freeze"))
        self.assertFalse(any(e[0] == "delete" or e[-1] == NEW for e in case.kube.events))
        self.assertFalse(case.scaling.protected)

    def test_abort_after_begin_accepted_but_reply_lost_replays_without_replacing_pods(self):
        case = self.fixture()
        begin = case.manager.begin
        accepted = []
        def lost(state):
            begin(state)
            accepted.append(case.manager.task)
            if len(accepted) == 1:
                raise UnknownOutcome("begin persisted but response lost")
            return case.manager.status(state)
        case.manager.begin = lost
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        self.assertIsNotNone(case.manager.task)
        self.assertEqual(case.engine.state["phase"], "NeedsAttention")
        case.manager.restore_original = lambda state, node, pod: {
            "restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
            "node_session_id": "session-" + node,
        }
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual(len(accepted), 2)
        self.assertIs(accepted[0], accepted[1])
        self.assertFalse(any(e[0] == "delete" or e[-1] == NEW for e in case.kube.events))
        self.assertEqual({p["metadata"]["uid"] for p in case.kube.pods}, {"a-old", "b-old"})
        self.assertFalse(case.scaling.protected)

    def test_rollback_probe_retry_keeps_the_restored_pod(self):
        case = self.fixture()
        case.probe.error = UnknownOutcome("new-image probe failed")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        case.manager.rollback = lambda state, node, pod: case.manager.quiesce(state, node)
        case.manager.restore_original = lambda state, node, pod: {
            "restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
            "node_session_id": "session-" + node,
        }
        case.probe.cleanup = lambda grant: None
        case.probe.error = UnknownOutcome("restored-image probe interrupted")
        with self.assertRaises(UnknownOutcome):
            case.engine.abort()
        restored = next(p for p in case.kube.pods if p["spec"]["nodeName"] == "a")
        deleted = [e for e in case.kube.events if e[0] == "delete"]
        case.probe.error = None
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual([e for e in case.kube.events if e[0] == "delete"], deleted)
        self.assertIn(restored["metadata"]["uid"], [p["metadata"]["uid"] for p in case.kube.pods])
        self.assertEqual(case.engine.state["nodes"][0]["old_uid"], "a-old")

    def test_rollback_delete_reply_loss_never_deletes_the_replacement(self):
        for accepted in (False, True):
            case = self.fixture()
            case.probe.error = UnknownOutcome("new-image probe failed")
            with self.assertRaises(UnknownOutcome):
                case.engine.apply()
            case.manager.rollback = lambda state, node, pod: case.manager.quiesce(state, node)
            case.manager.restore_original = lambda state, node, pod: {
                "restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
                "node_session_id": "session-" + node,
            }
            case.probe.cleanup = lambda grant: None
            case.probe.error = None
            delete = case.kube.delete_pod
            def lost(pod):
                if accepted:
                    delete(pod)
                raise UnknownOutcome("rollback DELETE response lost")
            case.kube.delete_pod = lost
            with self.subTest(accepted=accepted), self.assertRaises(UnknownOutcome):
                case.engine.abort()
            case.kube.delete_pod = delete
            self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
            self.assertEqual([e[1] for e in case.kube.events if e[0] == "delete"],
                             ["a-old", "a-old-new"])
            self.assertEqual(case.engine.state["nodes"][0]["old_uid"], "a-old")
            self.assertEqual(any(e.startswith("revoke-delete-") for e in case.manager.events),
                             not accepted)

    def test_authoritative_terminal_requires_exact_task_identity(self):
        for key, value in (("task_id", "other"), ("epoch", 2), ("namespace", "other"),
                           ("daemonset_uid", "other"), ("target_image", OLD)):
            case = self.fixture()
            self.terminal_manager(case, "Completed")
            with self.assertRaises(UnknownOutcome):
                case.engine.apply()
            case.manager.task[key] = value
            before = len(case.kube.events)
            with self.subTest(field=key), self.assertRaises(Conflict):
                self.resumed(case).run()
            self.assertEqual(len(case.kube.events), before)

    def test_successful_canary_cleanup_is_confirmed_before_quiesce(self):
        case = self.fixture()
        case.engine.apply()
        first = case.manager.events.index("revoke-canary-test")
        self.assertLess(first, case.manager.events.index("quiesce", first))

    def test_validation_uid_change_cleans_old_grant_before_overwriting_it(self):
        case = self.fixture()
        case.probe.error = UnknownOutcome("accepted create response lost")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        node = next(p for p in case.kube.pods if p["spec"]["nodeName"] == "a")
        node["metadata"]["uid"] += "-restart"
        case.manager.canary = lambda *args: {"ready": True, "environment_id": "new-test"}
        case.probe.error = None
        case.engine.validate_node("a", node["metadata"]["uid"])
        self.assertLess(case.manager.events.index("revoke-canary-test"),
                        case.manager.events.index("revoke-canary-new-test"))

    def test_abort_reconciles_canary_whose_authorization_reply_was_lost(self):
        case = self.fixture()
        def lost(*args):
            case.manager.task["canaries"] = {"default-orphan": "a"}
            raise UnknownOutcome("canary authorization reply lost before grant save")
        case.manager.canary = lost
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        case.manager.rollback = lambda state, node, pod: case.manager.quiesce(state, node)
        case.manager.restore_original = lambda state, node, pod: {
            "restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
            "node_session_id": "session-" + node,
        }
        case.manager.canary = lambda *args: {"ready": True, "environment_id": "new-test"}
        self.assertEqual(case.engine.abort()["phase"], "Aborted")
        self.assertLess(case.manager.events.index("freeze"),
                        case.manager.events.index("revoke-canary-default-orphan"))
        self.assertLess(case.manager.events.index("revoke-canary-default-orphan"),
                        case.manager.events.index("revoke-canary-new-test"))

    def test_cleanup_unconfirmed_blocks_replacement_validation_and_preserves_grant(self):
        case = self.fixture()
        case.probe.error = UnknownOutcome("accepted create response lost")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        old_record = copy.deepcopy(case.engine.state["validations"]["a"])
        case.manager.revoke_canary = lambda state, identity: {
            "environment_id": identity, "revoked": True, "creation_fenced": True,
            "cleanup_confirmed": False}
        with self.assertRaises(UnknownOutcome):
            case.engine.validate_node("a", "new-pod")
        self.assertEqual(case.engine.state["validations"]["a"], old_record)

    def test_cleanup_polls_same_identity_and_rejects_missing_creation_fence(self):
        case = self.fixture()
        case.probe.error = UnknownOutcome("accepted create response lost")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        calls = []
        def pending(state, identity):
            calls.append(identity)
            return {"environment_id": identity, "revoked": True, "creation_fenced": True,
                    "cleanup_confirmed": len(calls) == 2}
        case.manager.revoke_canary = pending
        case.engine.cleanup_canary({"environment_id": "test"})
        self.assertEqual(calls, ["test", "test"])
        case.manager.revoke_canary = lambda state, identity: {
            "environment_id": identity, "revoked": True, "cleanup_confirmed": True}
        with self.assertRaises(UnknownOutcome):
            case.engine.cleanup_canary({"environment_id": "test"})

    def test_abort_reverts_template_before_waiting_for_canary_cleanup(self):
        case = self.fixture()
        case.probe.error = UnknownOutcome("accepted create response lost")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        case.manager.revoke_canary = lambda state, identity: {
            "environment_id": identity, "revoked": True, "creation_fenced": True,
            "cleanup_confirmed": False}
        deleted = [e for e in case.kube.events if e[0] == "delete"]
        with self.assertRaises(UnknownOutcome):
            case.engine.abort()
        container = case.kube.ds["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], OLD)
        self.assertEqual(case.engine.state["phase"], "Aborting")
        self.assertEqual(container["env"], [{"name": "ADX_NODE_UPGRADE_ISOLATED", "value": "true"}])
        self.assertEqual([e for e in case.kube.events if e[0] == "delete"], deleted)

    def test_rollback_authorization_reply_loss_uses_new_canonical_after_freeze(self):
        case = self.fixture()
        revoked, authorized = set(), []
        class HTTP:
            def request(self, method, path, body=None):
                if method == "GET" and "/tasks/" in path:
                    task = copy.deepcopy(case.manager.task)
                    for node in task["nodes"].values():
                        node.setdefault("control_revision", 0)
                    return task
                if method == "GET":
                    return {"node_session_id": "session-b"}
                identity = "default-" + body["environment_id"]
                if identity in revoked:
                    raise Conflict("canary authorization was revoked")
                authorized.append(identity)
                case.manager.task.setdefault("canaries", {})[identity] = body["target_node_id"]
                if len(authorized) == 2:
                    raise UnknownOutcome("rollback authorization persisted; reply lost")
                return {"ready": True, "environment_id": identity, "name": body["environment_id"]}
        case.manager.canary = Management(HTTP()).canary
        freeze = case.manager.freeze
        def frozen(state):
            freeze(state)
            revoked.update(case.manager.task.get("canaries", {}))
        case.manager.freeze = frozen
        revoke = case.manager.revoke_canary
        def cleanup(state, identity):
            revoked.add(identity)
            return revoke(state, identity)
        case.manager.revoke_canary = cleanup
        case.manager.rollback = lambda state, node, pod: case.manager.quiesce(state, node)
        case.manager.restore_original = lambda state, node, pod: {
            "restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
            "node_session_id": "session-" + node,
        }
        case.probe.error = UnknownOutcome("new-image probe failed")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        case.probe.error = None
        with self.assertRaises(UnknownOutcome):
            case.engine.abort()
        self.assertEqual(case.engine.state["validations"]["a"]["phase"], "Authorizing")
        restored_uid = case.engine.state["validations"]["a"]["pod_uid"]
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual(len(authorized), 3)
        self.assertEqual(len(set(authorized)), 3)
        self.assertEqual(case.engine.state["validations"]["a"]["pod_uid"], restored_uid)


class LateSourceTests(unittest.TestCase):
    def fixture(self):
        case = EngineTests()
        case.setUp()
        resources = case.inventory.resources
        case.inventory.resources = lambda: {**resources(), **{
            pod["spec"]["nodeName"]: copy.deepcopy(resources()["a"])
            for pod in case.kube.pods if pod["spec"]["nodeName"] not in ("a", "b")}}
        return case

    @staticmethod
    def add_old_source(case, name="d"):
        pod = case.kube.pod(name, name + "-old")
        pod["status"]["containerStatuses"][0]["imageID"] = OLD
        case.kube.pods.append(pod)
        case.inventory.records.append({"instanceID": "work-" + name, "node_id": name,
                                       "status": "running", "generation": 1, "revision": 1})

    @staticmethod
    def observation(name="d"):
        return {"name": name, "uid": "node-" + name, "pool_id": "pool",
                "instance_id": "i-" + name, "ready_for_validation": True}

    @staticmethod
    def after_new_template(case, callback):
        patch = case.kube.patch
        def publish(kind, obj, operations):
            result = patch(kind, obj, operations)
            if kind == "daemonsets" and any(op["path"].endswith("/image") and op["value"] == NEW
                                            for op in operations):
                callback()
            return result
        case.kube.patch = publish

    def test_handoff_baseline_includes_already_completed_ordinary_expansion(self):
        case = self.fixture()
        protect = case.scaling.protect
        def ready(state):
            protect(state)
            self.add_old_source(case)
            case.scaling.protect = protect
        case.scaling.protect = ready
        result = case.engine.apply()
        self.assertEqual(result["phase"], "Completed")
        self.assertTrue(result["baseline_frozen"])
        self.assertEqual([n["node_id"] for n in result["nodes"]], ["a", "b", "d"])
        self.assertIn("d", case.manager.task["nodes"])
        self.assertIn("prepare-d", case.manager.events)
        self.assertFalse(any(r["node_id"] == "d" for r in case.inventory.records))

    def test_baseline_is_not_recaptured_after_begin_response_loss(self):
        case = self.fixture()
        begin = case.manager.begin
        def lost(state):
            begin(state)
            raise UnknownOutcome("begin persisted; reply lost")
        case.manager.begin = lost
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        self.assertTrue(case.engine.state["baseline_frozen"])
        self.add_old_source(case, "unknown")
        case.manager.begin = begin
        with self.assertRaises(Blocked):
            case.engine.run()
        self.assertEqual([n["node_id"] for n in case.engine.state["nodes"]], ["a", "b"])
        self.assertNotIn("prepare-unknown", case.manager.events)

    def test_baseline_refresh_preserves_original_pod_and_session_fences(self):
        for change in ("pod", "session"):
            case = self.fixture()
            protect = case.scaling.protect
            def changed(state):
                protect(state)
                if change == "pod":
                    case.kube.pods[0]["metadata"]["uid"] = "changed-pod"
                else:
                    inspect = case.manager.inspect
                    case.manager.inspect = lambda name: ({"node_session_id": "changed-session"}
                                                          if name == "a" else inspect(name))
            case.scaling.protect = changed
            with self.subTest(change=change), self.assertRaises(Conflict):
                case.engine.apply()
            self.assertEqual(case.engine.state["nodes"][0]["old_uid"], "a-old")
            self.assertEqual(case.engine.state["nodes"][0]["node_session_id"], "session-a")
            self.assertNotIn("begin", case.manager.events)

    def test_attributed_old_pod_is_evacuated_and_upgraded_as_a_source(self):
        case = self.fixture()
        self.after_new_template(case, lambda: self.add_old_source(case))
        case.scaling.ready_nodes = lambda _, **kwargs: ([self.observation()]
            if any(p["spec"]["nodeName"] == "d" for p in case.kube.pods) else [])
        result = case.engine.apply()
        late = next(n for n in result["nodes"] if n["node_id"] == "d")
        self.assertTrue(late["register_source"])
        self.assertEqual(late["phase"], "Done")
        self.assertTrue(late["migrations"]["work-d"]["completed"])
        self.assertIn(("delete", "d-old"), case.kube.events)

    def test_unregistered_old_source_is_not_used_as_a_healthy_canary_peer(self):
        case = self.fixture()
        self.after_new_template(case, lambda: self.add_old_source(case))
        case.scaling.ready_nodes = lambda _, **kwargs: ([self.observation()]
            if any(p["spec"]["nodeName"] == "d" for p in case.kube.pods) else [])
        canary = case.manager.canary
        def known_peer(state, target, uid, source, *args):
            if source not in case.manager.task["nodes"]:
                raise Conflict("healthy canary peer has no task binding")
            return canary(state, target, uid, source, *args)
        case.manager.canary = known_peer
        self.assertEqual(case.engine.apply()["phase"], "Completed")

    def test_old_source_discovered_during_finalizing_returns_to_the_same_upgrade_queue(self):
        case = self.fixture()
        finalized = []
        def finalize(state):
            finalized.append(state["index"])
            if len(finalized) == 1:
                self.add_old_source(case)
                raise Blocked("late ordinary expansion became visible")
        case.scaling.finalize = finalize
        case.scaling.ready_nodes = lambda _, **kwargs: ([self.observation()]
            if any(p["spec"]["nodeName"] == "d" for p in case.kube.pods) else [])
        self.assertEqual(case.engine.apply()["phase"], "Completed")
        self.assertEqual(finalized, [2, 3])
        self.assertIn("prepare-d", case.manager.events)

    def test_stale_node_uid_cannot_authorize_old_source_or_canary(self):
        case = self.fixture()
        self.after_new_template(case, lambda: self.add_old_source(case))
        case.scaling.ready_nodes = lambda _, **kwargs: ([{**self.observation(), "uid": "stale-node"}]
            if any(p["spec"]["nodeName"] == "d" for p in case.kube.pods) else [])
        with self.assertRaises(Conflict):
            case.engine.apply()
        self.assertNotIn("prepare-d", case.manager.events)
        self.assertEqual(case.probe.runs, 0)

    def test_abort_discovers_old_source_during_finalizing_and_restores_without_migration(self):
        case = self.fixture()
        case.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        case.manager.task["operations"].clear()
        restored = []
        def restore(state, node, pod):
            restored.append(node)
            return {"restored_original": True, "node_id": node, "pod_uid": pod["metadata"]["uid"],
                    "node_session_id": "session-" + node}
        case.manager.restore_original = restore
        finalized = []
        def finalize(state):
            finalized.append(True)
            if len(finalized) == 1:
                self.add_old_source(case)
                raise Blocked("ordinary expansion became visible while aborting")
        case.scaling.finalize = finalize
        case.scaling.ready_nodes = lambda _, **kwargs: ([self.observation()]
            if any(p["spec"]["nodeName"] == "d" for p in case.kube.pods) else [])
        self.assertEqual(case.engine.abort()["phase"], "Aborted")
        self.assertEqual(restored, ["a", "b", "d"])
        self.assertNotIn("prepare-d", case.manager.events)
        self.assertTrue(any(r["instanceID"] == "work-d" and r["node_id"] == "d"
                            for r in case.inventory.records))
        self.assertFalse(any(event[0] == "delete" for event in case.kube.events))
        self.assertEqual(case.probe.runs, 0)

    @staticmethod
    def use_bridge(case):
        case.kube.create_configmap("guard", {})
        case.scaling = Scaling(case.kube, "guard", "cluster", "pool")
        case.engine.scaling = case.scaling
        def observe(**extra):
            request = json.loads(case.kube.get("configmaps", "guard")["data"].get("request.json", "null"))
            phase = request["phase"] if request else "Active"
            status = {"version": 1, "cluster_id": "cluster", "observed_task_id": "task",
                      "observed_epoch": 1, "handoff": "Released" if phase == "Released" else "Ready",
                      "observed_phase": phase, "observed_nodes": [], "capacity_requests": [],
                      "unsettled_up": False, **extra}
            obj = case.kube.get("configmaps", "guard")
            case.kube.patch("configmaps", obj, [{"op": "add", "path": "/data/status.json",
                                                "value": json.dumps(status)}])
        observe()
        return observe

    def test_visible_unready_old_pod_holds_active_and_can_request_evacuation_capacity(self):
        case = self.fixture()
        observe = self.use_bridge(case)
        tight, accepted, waited = False, False, False
        original_resources = case.inventory.resources
        def resources():
            result = original_resources()
            if tight:
                for name in ("a", "b"):
                    result[name]["allocatable"] = {"CPU": 1000, "Memory": 1500}
            if "d" in result:
                result["d"]["allocatable"]["CPU"] = 8000
            if "e" in result and not accepted:
                result["e"] = {"status": 1, "capacity": {"CPU": 0, "Memory": 0},
                               "allocatable": {"CPU": 0, "Memory": 0}}
            return result
        case.inventory.resources = resources
        def add_pending():
            self.add_old_source(case)
            pod = case.kube.pods[-1]
            pod["spec"]["containers"] = [{"name": "akernel-node", "image": OLD}]
            pod["status"]["containerStatuses"][0].update(ready=False, imageID="")
            observe(observed_nodes=[{**self.observation(), "ready_for_validation": False}], unsettled_up=True)
        self.after_new_template(case, add_pending)
        verify = case.manager.verify
        def opened(*args):
            nonlocal accepted
            verify(*args)
            if args[1] == "e":
                accepted = True
        case.manager.verify = opened
        def tick(seconds):
            nonlocal tight, waited
            case.now += seconds
            request = json.loads(case.kube.get("configmaps", "guard")["data"]["request.json"])
            if not waited:
                self.assertEqual(request["phase"], "Active")
                waited, tight = True, True
                pod = next(p for p in case.kube.pods if p["spec"]["nodeName"] == "d")
                pod["status"]["containerStatuses"][0].update(ready=True, imageID=OLD)
            if request["capacity_requests"] and not any(p["spec"]["nodeName"] == "e" for p in case.kube.pods):
                self.assertEqual(request["phase"], "Active")
                case.kube.pods.append(case.kube.pod("e", "e-new"))
            added = any(p["spec"]["nodeName"] == "e" for p in case.kube.pods)
            nodes = [self.observation()] + ([self.observation("e")] if added else [])
            receipts = [{**entry, "phase": "Completed" if accepted else "ReadyForValidation",
                         "nodes": [self.observation("e")] if added else []}
                        for entry in request["capacity_requests"]]
            observe(observed_nodes=nodes, capacity_requests=receipts, unsettled_up=not accepted)
        case.engine.sleep = tick
        self.assertEqual(case.engine.apply()["phase"], "Completed")
        self.assertTrue(waited)
        request = json.loads(case.kube.get("configmaps", "guard")["data"]["request.json"])
        self.assertEqual(len(request["capacity_requests"]), 1)
        self.assertIn("prepare-d", case.manager.events)

    def test_unready_new_image_node_does_not_hold_active_before_finalizing(self):
        case = self.fixture()
        case.engine.apply()
        observe = self.use_bridge(case)
        case.engine.state["phase"] = "Running"
        pod = case.kube.pod("d", "d-new")
        pod["spec"]["containers"] = [{"name": "akernel-node", "image": NEW}]
        pod["status"]["containerStatuses"][0]["ready"] = False
        case.kube.pods.append(pod)
        observe(observed_nodes=[{**self.observation(), "ready_for_validation": False},
                                {"instance_id": "future", "pool_id": "pool", "ready_for_validation": False}],
                unsettled_up=True)
        saw_finalizing = []
        def tick(seconds):
            case.now += seconds
            request = json.loads(case.kube.get("configmaps", "guard")["data"]["request.json"])
            saw_finalizing.append(request["phase"])
            self.assertEqual(request["phase"], "Finalizing")
            pod["status"]["containerStatuses"][0]["ready"] = True
            observe(observed_nodes=[self.observation()],
                    unsettled_up="d" not in case.engine.state.get("validated_capacity_nodes", {}))
        case.engine.sleep = tick
        self.assertTrue(case.engine.finish_scaling())
        self.assertTrue(saw_finalizing)
        self.assertEqual([n["node_id"] for n in case.engine.state["nodes"]], ["a", "b"])
        self.assertIn("d", case.engine.state["validated_capacity_nodes"])

    def test_new_pod_readiness_cannot_bypass_scaler_confirmation(self):
        case = self.fixture()
        case.engine.apply()
        observe = self.use_bridge(case)
        case.engine.state["phase"] = "Running"
        case.kube.pods.append(case.kube.pod("d", "d-new"))
        observe(observed_nodes=[{**self.observation(), "ready_for_validation": False}], unsettled_up=True)
        polls = []
        def tick(seconds):
            case.now += seconds
            if not polls:
                self.assertNotIn("d", case.engine.state.get("validated_capacity_nodes", {}))
            polls.append(True)
            observe(observed_nodes=[self.observation()],
                    unsettled_up="d" not in case.engine.state.get("validated_capacity_nodes", {}))
        case.engine.sleep = tick
        self.assertTrue(case.engine.finish_scaling())
        self.assertTrue(polls)

    def test_unready_old_pod_timeout_leaves_request_active(self):
        case = self.fixture()
        observe = self.use_bridge(case)
        def pending():
            self.add_old_source(case)
            pod = case.kube.pods[-1]
            pod["spec"]["containers"] = [{"name": "akernel-node", "image": OLD}]
            pod["status"]["containerStatuses"][0]["ready"] = False
            observe(observed_nodes=[{**self.observation(), "ready_for_validation": False}], unsettled_up=True)
        self.after_new_template(case, pending)
        with self.assertRaises(Blocked):
            case.engine.apply()
        request = json.loads(case.kube.get("configmaps", "guard")["data"]["request.json"])
        self.assertEqual(request["phase"], "Active")
        self.assertEqual(case.now, case.settings.node_timeout_seconds)


class PrewarmCapacityTests(unittest.TestCase):
    def fixture(self):
        case = LateSourceTests().fixture()
        case.settings = replace(case.settings, prewarm_capacity=True, node_timeout_seconds=60)
        case.engine.cfg = case.settings
        observe = LateSourceTests.use_bridge(case)
        prepare = case.manager.prepare
        def guarded_prepare(state, node):
            self.assertIn("d", case.engine.state["validated_capacity_nodes"])
            request, status = self.channel(case)
            self.assertEqual(len(request["capacity_requests"]), 1)
            self.assertEqual(status["capacity_requests"][0]["phase"], "Completed")
            return prepare(state, node)
        case.manager.prepare = guarded_prepare
        def tick(seconds):
            case.now += seconds
            request, _ = self.channel(case)
            if request["capacity_requests"] and not any(p["spec"]["nodeName"] == "d" for p in case.kube.pods):
                self.assertEqual(case.kube.ds["spec"]["template"]["spec"]["containers"][0]["image"], NEW)
                self.assertFalse(any(e.startswith("prepare-") for e in case.manager.events))
                case.kube.pods.append(case.kube.pod("d", "d-new"))
            added = any(p["spec"]["nodeName"] == "d" for p in case.kube.pods)
            accepted = "d" in case.manager.verified
            nodes = [{**LateSourceTests.observation(), "accepting": accepted}] if added else []
            receipts = [{**entry, "phase": "Completed" if accepted else "ReadyForValidation",
                         "nodes": nodes} for entry in request["capacity_requests"]]
            observe(observed_nodes=nodes, capacity_requests=receipts, unsettled_up=added and not accepted)
        case.engine.sleep = tick
        return case, observe

    @staticmethod
    def channel(case):
        data = case.kube.get("configmaps", "guard")["data"]
        return json.loads(data.get("request.json", "null")), json.loads(data["status.json"])

    def test_explicit_prewarm_delivers_and_validates_before_first_source_is_touched(self):
        case, _ = self.fixture()
        self.assertEqual(case.engine.apply()["phase"], "Completed")
        self.assertEqual(case.probe.runs, 3)
        self.assertEqual(len(self.channel(case)[0]["capacity_requests"]), 1)
        self.assertTrue(case.engine.state["configuration"]["prewarm_capacity"])
        self.assertTrue(case.engine.state["capacity_requested"])

    def test_resume_after_completed_prewarm_reuses_the_same_request(self):
        case, _ = self.fixture()
        prepare = case.manager.prepare
        case.manager.prepare = lambda *args: (_ for _ in ()).throw(UnknownOutcome("Job interrupted"))
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        before = copy.deepcopy(self.channel(case)[0]["capacity_requests"])
        self.assertEqual(self.channel(case)[1]["capacity_requests"][0]["phase"], "Completed")
        case.manager.prepare = prepare
        case.engine.load()
        self.assertEqual(case.engine.run()["phase"], "Completed")
        self.assertEqual(self.channel(case)[0]["capacity_requests"], before)
        self.assertEqual(case.probe.runs, 3)

    def test_lost_prewarm_request_reply_never_touches_a_source_or_buys_twice(self):
        case, _ = self.fixture()
        prewarm = case.scaling.prewarm_capacity
        def lost(state):
            prewarm(state)
            raise UnknownOutcome("request write response lost")
        case.scaling.prewarm_capacity = lost
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        self.assertFalse(any(e.startswith("prepare-") for e in case.manager.events))
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))
        before = copy.deepcopy(self.channel(case)[0]["capacity_requests"])
        case.scaling.prewarm_capacity = prewarm
        self.assertEqual(case.engine.run()["phase"], "Completed")
        self.assertEqual(self.channel(case)[0]["capacity_requests"], before)

    def test_resume_after_scaler_finalizing_keeps_completed_prewarm_receipt(self):
        case, _ = self.fixture()
        tick = case.engine.sleep
        interrupted = False
        def stop_after_finalizing(seconds):
            nonlocal interrupted
            request, _ = self.channel(case)
            if request["phase"] == "Finalizing" and not interrupted:
                interrupted = True
                raise UnknownOutcome("Job interrupted while finalizing")
            tick(seconds)
        case.engine.sleep = stop_after_finalizing
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        self.assertEqual(self.channel(case)[0]["phase"], "Finalizing")
        self.assertTrue(all(n["phase"] == "Done" for n in case.engine.state["nodes"]))
        before = copy.deepcopy(self.channel(case)[0]["capacity_requests"])
        case.engine.load()
        self.assertEqual(case.engine.run()["phase"], "Completed")
        self.assertEqual(self.channel(case)[0]["capacity_requests"], before)

    def test_undelivered_capacity_times_out_with_old_pods_and_protection_retained(self):
        for phase in ("Pending", "Blocked", "Unknown"):
            with self.subTest(phase=phase):
                case, observe = self.fixture()
                def tick(seconds):
                    case.now += seconds
                    request, _ = self.channel(case)
                    observe(capacity_requests=[{**entry, "phase": phase, "nodes": []}
                                              for entry in request["capacity_requests"]])
                case.engine.sleep = tick
                with self.assertRaisesRegex(Blocked, "prewarm capacity is not delivered"):
                    case.engine.apply()
                self.assertEqual(case.now, 60)
                self.assertEqual(case.engine.state["phase"], "NeedsAttention")
                self.assertEqual([p["metadata"]["uid"] for p in case.kube.pods], ["a-old", "b-old"])
                self.assertFalse(any(e.startswith("prepare-") for e in case.manager.events))
                request, _ = self.channel(case)
                self.assertEqual(request["phase"], "Active")
                self.assertEqual(len(request["capacity_requests"]), 1)

    def test_failed_prewarm_canary_never_touches_sources(self):
        case, _ = self.fixture()
        case.probe.error = UnknownOutcome("canary unavailable")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        self.assertFalse(any(e.startswith("prepare-") for e in case.manager.events))
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))
        self.assertEqual(self.channel(case)[0]["phase"], "Active")

    def test_completed_receipt_without_delivered_nodes_never_touches_sources(self):
        case, observe = self.fixture()
        def tick(seconds):
            case.now += seconds
            request, _ = self.channel(case)
            observe(capacity_requests=[{**entry, "phase": "Completed", "nodes": []}
                                      for entry in request["capacity_requests"]])
        case.engine.sleep = tick
        with self.assertRaisesRegex(UnknownOutcome, "unique delivered nodes"):
            case.engine.apply()
        self.assertFalse(any(e.startswith("prepare-") for e in case.manager.events))
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))

    def test_false_default_preserves_legacy_configuration_and_cannot_be_enabled_on_resume(self):
        case = EngineTests()
        case.setUp()
        case.manager.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        configuration = case.engine.state["configuration"]
        self.assertNotIn("prewarm_capacity", configuration)
        case.engine.load()
        self.assertEqual(case.engine.task_configuration(), configuration)
        case.engine.cfg = replace(case.settings, prewarm_capacity=True)
        with self.assertRaises(Conflict):
            case.engine.load()

    def test_prewarm_configuration_rejects_non_boolean_values(self):
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(Blocked):
                replace(Settings("test", "node", NEW, "task", "executor",
                                 checkpoint_storage_alias="shared"), prewarm_capacity=value).validate()

    def test_once_request_keeps_completed_receipt_while_reactive_requests_can_advance(self):
        case, observe = self.fixture()
        state = {"task_id": "task", "epoch": 1}
        case.scaling.protect(state)
        self.assertIsNone(case.scaling.prewarm_capacity(state))
        request, _ = self.channel(case)
        receipt = {**request["capacity_requests"][0], "phase": "Completed", "nodes": []}
        observe(capacity_requests=[receipt])
        self.assertEqual(case.scaling.prewarm_capacity(state), receipt)
        self.assertEqual(case.scaling.prewarm_capacity(state), receipt)
        self.assertEqual(len(self.channel(case)[0]["capacity_requests"]), 1)
        case.scaling.request_capacity(state, {"node_id": "a"}, {}, {})
        request, _ = self.channel(case)
        reactive = {**request["capacity_requests"][-1], "phase": "Completed", "nodes": []}
        observe(capacity_requests=[receipt, reactive])
        case.scaling.request_capacity(state, {"node_id": "a"}, {}, {})
        self.assertEqual(len(self.channel(case)[0]["capacity_requests"]), 3)

    def test_completed_prewarm_is_reusable_after_finalizing_without_new_purchases(self):
        case, observe = self.fixture()
        state = {"task_id": "task", "epoch": 1}
        case.scaling.protect(state)
        case.scaling.prewarm_capacity(state)
        request, _ = self.channel(case)
        receipt = {**request["capacity_requests"][0], "phase": "Completed", "nodes": []}
        observe(capacity_requests=[receipt])
        with self.assertRaises(Blocked):
            case.scaling.finalize(state)
        self.assertEqual(case.scaling.prewarm_capacity(state), receipt)
        self.assertEqual(len(self.channel(case)[0]["capacity_requests"]), 1)
        with self.assertRaises(Blocked):
            case.scaling.request_capacity(state, {"node_id": "a"}, {}, {})

    def test_finalizing_cannot_create_a_missing_prewarm_request(self):
        case, observe = self.fixture()
        state = {"task_id": "task", "epoch": 1}
        case.scaling.protect(state)
        with self.assertRaises(Blocked):
            case.scaling.finalize(state)
        with self.assertRaisesRegex(Blocked, "cannot be extended"):
            case.scaling.prewarm_capacity(state)
        self.assertEqual(self.channel(case)[0]["capacity_requests"], [])


class CanaryTests(unittest.TestCase):
    def fixture(self, fail_delete=False):
        events, files = [], {}
        class Files:
            def write(self, path, value): files[path] = value
            def read(self, path):
                self_outer.assertIn("migrated", events)
                return files[path]
        class Commands:
            def run(self, command):
                self_outer.assertIn("migrated", events)
                return type("Result", (), {"exit_code": 0, "stdout": next(iter(files.values()))})()
        class Sandbox:
            def __init__(self, **kwargs):
                events.append(kwargs)
                self.id = "default-canary"
                self.files, self.commands = Files(), Commands()
            def kill(self):
                events.append("deleted")
                if fail_delete: raise RuntimeError("lost delete")
        class Manager:
            def migrate(self, state, node, record, operation_id, **extra):
                self_outer.assertEqual(node, "b")
                self_outer.assertEqual(record["instanceID"], "default-canary")
                self_outer.assertEqual(extra["target_node_id"], "a")
                events.append("migrated")
                return {"outcome": "completed", "state": "running", "node_id": "a"}
        self_outer = self
        sdk = type("SDK", (), {"Sandbox": Sandbox})
        canary = SDKCanary(Manager(), sdk=sdk)
        canary.preflight = lambda: []
        grant = {"ready": True, "environment_id": "default-canary", "name": "canary",
                 "source_node_id": "b", "checkpoint_storage_alias": "shared"}
        return canary, grant, events

    def test_sdk_targets_source_then_probes_same_identity_after_shared_restore(self):
        canary, grant, events = self.fixture()
        canary.run({"task_id": "task"}, "a", grant)
        self.assertEqual(events[0]["node_id"], "b")
        self.assertEqual(events[0]["name"], "canary")
        self.assertEqual(events[-2:], ["migrated", "deleted"])

    def test_unconfirmed_canary_cleanup_is_unknown(self):
        canary, grant, events = self.fixture(fail_delete=True)
        with self.assertRaises(UnknownOutcome):
            canary.run({"task_id": "task"}, "a", grant)


if __name__ == "__main__":
    unittest.main()
