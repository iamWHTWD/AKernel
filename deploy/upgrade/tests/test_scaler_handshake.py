"""Cross-component handoff and restart regressions using local fakes only."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_upgrade import (FakeCanary, FakeInventory, FakeKube, FakeManagement,
                          FakeScaling, NEW, JOB_IDENTITY)
from akernel_upgrade.engine import Executor, Settings
from akernel_upgrade.errors import Blocked, UnknownOutcome
from akernel_upgrade.protocol import Management, Scaling


class ScalerHandshakeTests(unittest.TestCase):
    def setUp(self):
        self.kube = FakeKube()
        self.state = {"task_id": "task", "epoch": 1}
        self.kube.create_configmap("guard", {})
        self.scaling = Scaling(self.kube, "guard", "cluster", "pool")
        self.observe()
        self.scaling.protect(self.state)

    def observe(self, handoff="Ready", requests=(), state=None, *, observed_nodes=(),
                observed_phase="Active", unsettled_up=False):
        identity = state or self.state
        obj = self.kube.get("configmaps", "guard")
        self.kube.patch("configmaps", obj, [{"op": "add", "path": "/data/status.json", "value": json.dumps({
            "version": 1, "cluster_id": "cluster", "observed_task_id": identity["task_id"],
            "observed_epoch": identity["epoch"], "handoff": handoff,
            "capacity_requests": list(requests), "observed_phase": observed_phase,
            "observed_nodes": list(observed_nodes), "unsettled_up": unsettled_up})}])

    def request(self):
        return json.loads(self.kube.get("configmaps", "guard")["data"]["request.json"])

    def test_release_waits_for_exact_ack_before_next_task_can_replace_request(self):
        with self.assertRaises(Blocked):
            self.scaling.release(self.state)
        self.assertEqual(self.request()["phase"], "Released")
        next_task = {"task_id": "next", "epoch": 1}
        with self.assertRaises(Blocked):
            self.scaling.protect(next_task)
        self.assertEqual(self.request()["task_id"], "task")
        self.observe("Released", state={"task_id": "task", "epoch": 2})
        with self.assertRaises(Blocked):
            self.scaling.release(self.state)
        self.observe("Released")
        self.scaling.release(self.state)
        with self.assertRaises(Blocked):
            self.scaling.protect(next_task)  # New Active still needs its own Ready ACK.
        self.assertEqual(self.request()["task_id"], "next")

    def test_capacity_rounds_append_only_after_exact_completed_receipt(self):
        node = {"node_id": "a"}
        self.scaling.request_capacity(self.state, node, {}, {})
        first = copy.deepcopy(self.request()["capacity_requests"][0])
        for phase in (None, "Pending", "Provisioning", "ReadyForValidation", "Blocked"):
            self.observe(requests=[] if phase is None else [{**first, "phase": phase}])
            self.scaling.request_capacity(self.state, node, {}, {})
            self.assertEqual(self.request()["capacity_requests"], [first])
        self.observe(requests=[{**first, "phase": "Completed"}])
        self.scaling.request_capacity(self.state, node, {}, {})
        two = copy.deepcopy(self.request()["capacity_requests"])
        self.assertEqual(len(two), 2)
        self.assertNotEqual(two[0]["request_id"], two[1]["request_id"])
        # A crash after appending round two, before its status arrives, cannot
        # turn the still-published round-one receipt into a third purchase.
        restarted = Scaling(self.kube, "guard", "cluster", "pool")
        restarted.request_capacity(self.state, node, {}, {})
        self.assertEqual(self.request()["capacity_requests"], two)
        self.observe(requests=[{**entry, "phase": "Completed"} for entry in two])
        restarted.request_capacity(self.state, node, {}, {})
        self.assertEqual(self.request()["capacity_requests"][:2], two)
        self.assertEqual(len(self.request()["capacity_requests"]), 3)

    def test_stale_completed_status_never_purchases_another_round(self):
        node = {"node_id": "a"}
        self.scaling.request_capacity(self.state, node, {}, {})
        first = copy.deepcopy(self.request()["capacity_requests"][0])
        self.observe(requests=[{**first, "phase": "Completed"}], state={"task_id": "task", "epoch": 2})
        with self.assertRaises(Blocked):
            self.scaling.request_capacity(self.state, node, {}, {})
        self.assertEqual(self.request()["capacity_requests"], [first])

    def test_finalizing_waits_for_requests_then_fences_and_settles_ordinary_expansion(self):
        self.scaling.request_capacity(self.state, {"node_id": "a"}, {}, {})
        entry = self.request()["capacity_requests"][0]
        for phase in ("Pending", "Blocked"):
            self.observe(requests=[{**entry, "phase": phase, "operation_ids": []}])
            with self.assertRaises(Blocked):
                self.scaling.finalize(self.state)
            self.assertEqual(self.request()["phase"], "Active")
        receipts = [{**entry, "phase": "Completed"}]
        self.observe(requests=receipts, unsettled_up=True)
        with self.assertRaises(Blocked):
            self.scaling.finalize(self.state)
        self.assertEqual(self.request()["phase"], "Finalizing")
        self.scaling.protect(self.state)
        self.assertEqual(self.request()["phase"], "Finalizing")
        self.observe(requests=receipts, observed_phase="Finalizing", unsettled_up=True)
        with self.assertRaises(Blocked):
            self.scaling.finalize(self.state)
        self.observe(requests=receipts, observed_phase="Finalizing", unsettled_up=False)
        self.scaling.finalize(self.state)

    def test_abort_finalizes_blocked_unsubmitted_capacity_without_buying_it(self):
        self.scaling.request_capacity(self.state, {"node_id": "a"}, {}, {})
        entry = self.request()["capacity_requests"][0]
        # A request beyond MaxNodes is Blocked without a cloud operation.
        receipts = [{**entry, "phase": "Blocked", "operation_ids": []}]
        self.observe(requests=receipts)
        self.state["phase"] = "Aborting"
        with self.assertRaises(Blocked):
            self.scaling.finalize(self.state)
        self.assertEqual(self.request()["phase"], "Finalizing")
        self.assertEqual(self.request()["capacity_requests"], [entry])
        self.observe(requests=receipts, observed_phase="Finalizing",
                     state={"task_id": "task", "epoch": 2})
        with self.assertRaises(Blocked):
            self.scaling.finalize(self.state)
        self.observe(requests=receipts, observed_phase="Finalizing")
        self.scaling.finalize(self.state)

    def test_abort_finalizing_still_waits_for_unknown_submitted_expansion(self):
        self.scaling.request_capacity(self.state, {"node_id": "a"}, {}, {})
        entry = self.request()["capacity_requests"][0]
        receipts = [{**entry, "phase": "Provisioning", "operation_ids": ["unknown-up"]}]
        self.observe(requests=receipts, unsettled_up=True)
        self.state["phase"] = "Aborting"
        with self.assertRaises(Blocked):
            self.scaling.finalize(self.state)
        self.assertEqual(self.request()["phase"], "Finalizing")
        self.observe(requests=receipts, observed_phase="Finalizing", unsettled_up=True)
        with self.assertRaises(Blocked):
            self.scaling.finalize(self.state)
        self.assertEqual(self.request()["phase"], "Finalizing")

    def test_ordinary_attributed_expansion_is_validated_without_capacity_request(self):
        self.kube.ds["spec"]["template"]["spec"]["containers"][0]["image"] = NEW
        self.kube.pods.append(self.kube.pod("d", "pod-d"))
        observed = [{"name": name, "uid": "node-" + name, "pool_id": "pool",
                     "instance_id": "i-" + name, "ready_for_validation": True, "accepting": False}
                    for name in ("a", "d")]
        self.observe(observed_nodes=observed, unsettled_up=True)
        engine = make_engine(self.kube, self.scaling)
        engine.state = {**self.state, "daemonset_uid": "ds", "nodes": [
            {"node_id": "a", "old_uid": "a-old"}, {"node_id": "b", "old_uid": "b-old"}]}
        engine.saved = self.kube.create_configmap("local", {"state.json": json.dumps(engine.state)})
        engine.validate_capacity_nodes()
        self.assertEqual(set(engine.state["validated_capacity_nodes"]), {"d"})
        self.assertEqual(engine.canary.runs, 1)
        # A same-owner Pod without a journal observation is never accepted.
        self.kube.pods.append(self.kube.pod("unknown", "unknown-pod"))
        for node in engine.state["nodes"]:
            node.update(phase="Done", new_uid=node["old_uid"])
        with self.assertRaises(Blocked):
            engine.final_inventory("Completed")

    def test_completed_capacity_receipt_can_rebuild_validation_inventory(self):
        self.kube.ds["spec"]["template"]["spec"]["containers"][0]["image"] = NEW
        pod = self.kube.pod("d", "pod-d")
        self.kube.pods.append(pod)
        node = {"name": "d", "uid": "node-d", "pool_id": "pool", "instance_id": "i-d",
                "ready_for_validation": True, "accepting": True}
        self.observe(requests=[{"request_id": "one", "pool_id": "pool", "count": 1,
                                "phase": "Completed", "nodes": [node]}])
        engine = make_engine(self.kube, self.scaling)
        engine.state = {**self.state, "nodes": [], "daemonset_uid": "ds", "validations": {
            "d": {"pod_uid": "pod-d", "node_session_id": "session-d", "phase": "Verified"}}}
        engine.saved = self.kube.create_configmap("local", {"state.json": json.dumps(engine.state)})
        engine.management.verified["d"] = {**self.state, "pod_uid": "pod-d",
                                             "node_session_id": "session-d", "phase": "verified"}
        for phase in ("Verifying", "Verified"):
            engine.state["validations"]["d"]["phase"] = phase
            engine.state["validated_capacity_nodes"] = {}
            engine.validate_capacity_nodes()
            self.assertEqual(engine.state["validated_capacity_nodes"]["d"],
                             {"node_uid": "node-d", "pod_uid": "pod-d"})
        self.assertEqual(engine.canary.runs, 0)


def make_engine(kube=None, scaling=None):
    kube = kube or FakeKube()
    inventory = FakeInventory()
    settings = Settings("test", "node", NEW, "task", "executor",
                        checkpoint_storage_alias="shared", node_timeout_seconds=180)
    now = [0]
    def sleep(seconds):
        now[0] += seconds
    management = FakeManagement(kube, inventory)
    def restore_original(state, node_id, pod):
        node = next(n for n in state["nodes"] if n["node_id"] == node_id)
        return {"restored_original": True, "node_id": node_id,
                "pod_uid": pod["metadata"]["uid"], "node_session_id": node["node_session_id"]}
    management.restore_original = restore_original
    return Executor(settings, kube, inventory, management, FakeCanary(), scaling or FakeScaling(),
                    execution_identity=JOB_IDENTITY, clock=lambda: now[0], sleep=sleep)


class EngineHandshakeTests(unittest.TestCase):
    def paused_source(self, raw):
        engine = make_engine()
        resources = engine.inventory.resources()
        resources["a"] = {"status": 1, "capacity": {"CPU": 0, "Memory": 0, "Disk": 0},
                          "allocatable": {"CPU": 0, "Memory": 0, "Disk": 0}}
        resources["b"]["capacity"]["Disk"] = 9000
        resources["b"]["allocatable"]["Disk"] = 9000
        engine.inventory.resources = lambda: resources
        engine.management.inspect = lambda _: {"resources": raw}
        engine.state = {"daemonset_uid": "ds", "capacity_requested": False}
        return engine

    def test_paused_source_uses_management_occupancy_and_rounds_bytes_up(self):
        mib = 1024 * 1024
        engine = self.paused_source({
            "capacity": {"cpu_millis": 8000, "memory_bytes": 8 * mib, "disk_bytes": 3 * mib},
            "allocatable": {"cpu_millis": 6700, "memory_bytes": 7 * mib - 1, "disk_bytes": 2 * mib - 1}})
        node = {"node_id": "a", "original_paused": True}
        engine.capacity(node)
        self.assertEqual(node["need"], {"CPU": 1300, "Memory": 2, "Disk": 2})
        self.assertTrue(node["original_paused"])
        self.assertEqual(engine.scaling.requests, 0)
        self.assertEqual(engine.management.events, [])

    def test_paused_source_never_uses_public_zero_when_management_ledger_is_invalid(self):
        valid = {"capacity": {"cpu_millis": 8000, "memory_bytes": 8192, "disk_bytes": 4096},
                 "allocatable": {"cpu_millis": 7000, "memory_bytes": 4096, "disk_bytes": 2048}}
        invalid = [None, {}, {"capacity": valid["capacity"]}]
        for field, key, value in (("capacity", "cpu_millis", True),
                                  ("capacity", "memory_bytes", -1),
                                  ("allocatable", "memory_bytes", 1.5),
                                  ("allocatable", "disk_bytes", 4097)):
            raw = copy.deepcopy(valid)
            raw[field][key] = value
            invalid.append(raw)
        for raw in invalid:
            engine = self.paused_source(raw)
            node = {"node_id": "a", "need": {"CPU": 5000}, "original_paused": True}
            with self.subTest(raw=raw), self.assertRaises(Blocked):
                engine.capacity(node)
            self.assertEqual(node["need"], {"CPU": 5000})
            self.assertTrue(node["original_paused"])
            self.assertEqual(engine.scaling.requests, 0)

    def test_apply_rejects_unready_or_mixed_original_images_before_handoff(self):
        for field, value in (("ready", False), ("imageID", NEW)):
            engine = make_engine()
            engine.kube.pods[0]["status"]["containerStatuses"][0][field] = value
            with self.assertRaises(Blocked):
                engine.apply()
            self.assertEqual(engine.kube.maps, {})
            self.assertEqual(engine.management.events, [])
            self.assertFalse(engine.scaling.protected)

    def test_lost_release_reply_preserves_completed_state_and_only_retries_release(self):
        engine = make_engine()
        def lost(_):
            raise UnknownOutcome("release reply lost")
        engine.scaling.release = lost
        with self.assertRaises(UnknownOutcome):
            engine.apply()
        self.assertEqual(engine.state["phase"], "Completed")
        begin_count = engine.management.events.count("begin")
        with self.assertRaises(UnknownOutcome):
            engine.run()
        self.assertEqual(engine.state["phase"], "Completed")
        self.assertEqual(engine.management.events.count("begin"), begin_count)

    def test_release_polls_until_ack_without_republishing_image(self):
        engine = make_engine()
        calls = []
        def pending(_):
            calls.append(True)
            if len(calls) < 3:
                raise Blocked("waiting for Released ACK")
        engine.scaling.release = pending
        self.assertEqual(engine.apply()["phase"], "Completed")
        self.assertEqual(len(calls), 3)
        self.assertEqual(engine.management.events.count("begin"), 1)

    def test_sufficient_business_capacity_still_waits_for_scaler_settlement_before_cleanup(self):
        engine = make_engine()
        attempts = []
        def settling(_):
            attempts.append(engine.clock())
            self.assertNotIn("Completed", engine.management.events)
            env = engine.kube.ds["spec"]["template"]["spec"]["containers"][0]["env"]
            self.assertIn({"name": "ADX_NODE_UPGRADE_ISOLATED", "value": "true"}, env)
            if len(attempts) < 3:
                raise Blocked("accepted capacity is not yet validated")
        engine.scaling.finalize = settling
        self.assertEqual(engine.apply()["phase"], "Completed")
        self.assertEqual(len(attempts), 3)

    def test_capacity_refresh_can_request_a_later_round_after_sixty_seconds(self):
        engine = make_engine()
        engine.inventory.available = False
        calls = []
        original = engine.scaling.request_capacity
        def request(*args):
            calls.append(engine.clock())
            original(*args)
        engine.scaling.request_capacity = request
        with self.assertRaises(Blocked):
            engine.apply()
        self.assertGreaterEqual(len(calls), 2)
        self.assertGreaterEqual(calls[0], 60)
        self.assertTrue(all(b - a >= 60 for a, b in zip(calls, calls[1:])))

    def test_abort_uses_current_replacement_pod_before_it_was_bound_by_canary(self):
        engine = make_engine()
        wait = engine.wait_replacement
        def pending(*_):
            raise Blocked("replacement not inspected yet")
        engine.wait_replacement = pending
        with self.assertRaises(Blocked):
            engine.apply()
        current = next(p for p in engine.kube.pods if p["spec"]["nodeName"] == "a")
        seen = []
        def rollback(state, node_id, pod):
            seen.append(pod["metadata"]["uid"])
            return engine.management.quiesce(state, node_id)
        engine.management.rollback = rollback
        engine.wait_replacement = wait
        self.assertEqual(engine.abort()["phase"], "Aborted")
        self.assertEqual(seen, [current["metadata"]["uid"]])

    def interrupted_original(self, completed=True):
        engine = make_engine()
        engine.management.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            engine.apply()
        node = engine.state["nodes"][0]
        pending = node["migrations"]["work"]
        engine.management.task["operations"][pending["operation_id"]]["phase"] = (
            "Completed" if completed else "Assigned")
        return engine

    def test_abort_reopens_original_pods_only_after_authoritative_ack(self):
        engine = self.interrupted_original()
        self.assertEqual(engine.abort()["phase"], "Aborted")
        self.assertTrue(all(node["phase"] == "RestoredOriginal" for node in engine.state["nodes"]))
        self.assertEqual(engine.canary.runs, 0)
        self.assertFalse(any(event[0] == "delete" for event in engine.kube.events))
        self.assertFalse(engine.scaling.protected)

    def test_abort_does_not_reopen_unknown_migration_or_unacknowledged_original(self):
        for completed in (False, True):
            engine = self.interrupted_original(completed)
            engine.management.restore_original = lambda *_: {}
            with self.assertRaises(UnknownOutcome):
                engine.abort()
            self.assertEqual(engine.state["phase"], "Aborting")
            self.assertEqual(engine.state["nodes"][0]["phase"], "Migrating")
            self.assertTrue(engine.scaling.protected)
            self.assertFalse(any(event[0] == "delete" for event in engine.kube.events))

    def test_rollback_protocol_sends_explicit_current_uid_and_rv(self):
        calls = []
        class HTTP:
            def request(self, method, path, body=None):
                calls.append((method, body))
                return {"node_session_id": "new-session"} if method == "GET" else {}
        pod = {"metadata": {"uid": "new-pod", "resourceVersion": "9"}}
        Management(HTTP()).rollback({"task_id": "task", "epoch": 1,
                                     "nodes": [{"node_id": "a", "old_uid": "old-pod"}]}, "a", pod)
        self.assertEqual(calls[-1][1]["pod_uid"], "new-pod")
        self.assertEqual(calls[-1][1]["pod_resource_version"], "9")
        self.assertEqual(calls[-1][1]["node_session_id"], "new-session")
        self.assertTrue(calls[-1][1]["rollback"])
        Management(HTTP()).restore_original({"task_id": "task", "epoch": 1,
                                             "nodes": [{"node_id": "a", "old_uid": "old-pod"}]}, "a", pod)
        self.assertEqual(calls[-1][1]["pod_uid"], "new-pod")
        self.assertEqual(calls[-1][1]["pod_resource_version"], "9")
        self.assertEqual(calls[-1][1]["stage"], "restore-original")

    def test_abort_canary_and_verify_carry_explicit_rollback_intent(self):
        calls = []
        class HTTP:
            def request(self, method, path, body=None):
                if body is not None:
                    calls.append(body)
                return {"node_session_id": "session"}
        management = Management(HTTP())
        state = {"task_id": "task", "epoch": 1, "phase": "Aborting", "nodes": []}
        management.action(state, "authorize-canary", "a", pod_uid="pod")
        management.action(state, "verify-node", "a", pod_uid="pod")
        self.assertTrue(all(body.get("rollback") is True for body in calls))
        state["phase"] = "Running"
        management.action(state, "verify-node", "a", pod_uid="pod")
        self.assertNotIn("rollback", calls[-1])


if __name__ == "__main__":
    unittest.main()
