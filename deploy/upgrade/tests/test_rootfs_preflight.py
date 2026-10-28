"""Reject incompatible rootfs before moving workloads or deleting a Pod."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_scaler_handshake import make_engine
from test_upgrade import NEW
from akernel_upgrade.errors import Blocked, UnknownOutcome


class RootfsPreflightTests(unittest.TestCase):
    def test_unknown_or_different_target_artifact_blocks_before_any_write(self):
        for identity in (None, {}, {"image": NEW, "runtime_rootfs_sha256": "bad"},
                         {"image": "another@sha256:" + "3" * 64,
                          "runtime_rootfs_sha256": "a" * 64}):
            engine = make_engine()
            engine.execution_identity = identity
            with self.subTest(identity=identity), self.assertRaises(Blocked):
                engine.apply()
            self.assertFalse(engine.kube.events)
            self.assertFalse(engine.management.events)
            self.assertFalse(engine.scaling.protected)

    def test_old_node_missing_or_changed_rootfs_blocks_before_any_write(self):
        for value in (None, "", "b" * 64):
            engine = make_engine()
            inspect = engine.management.inspect
            engine.management.inspect = lambda node: {
                **inspect(node), "runtime_rootfs_sha256": value}
            with self.subTest(value=value), self.assertRaises(Blocked):
                engine.apply()
            self.assertFalse(engine.kube.events)
            self.assertFalse(engine.management.events)

    def test_source_changes_between_plan_and_handoff_block_publication(self):
        engine = make_engine()
        protect = engine.scaling.protect
        inspect = engine.management.inspect
        def change(state):
            protect(state)
            engine.management.inspect = lambda node: {
                **inspect(node), "runtime_rootfs_sha256": "b" * 64}
        engine.scaling.protect = change
        with self.assertRaises(Blocked):
            engine.apply()
        self.assertNotIn("begin", engine.management.events)
        self.assertFalse(any(e[0] in ("daemonsets", "delete") for e in engine.kube.events))

    def test_replacement_rootfs_mismatch_never_creates_canary_or_opens_node(self):
        engine = make_engine()
        inspect = engine.management.inspect
        def changed(node):
            result = inspect(node)
            pod = next(p for p in engine.kube.pods if p["spec"]["nodeName"] == node)
            if pod["metadata"]["uid"].endswith("-new"):
                result["runtime_rootfs_sha256"] = "b" * 64
            return result
        engine.management.inspect = changed
        with self.assertRaises(Blocked):
            engine.apply()
        self.assertEqual(engine.canary.runs, 0)
        self.assertNotIn("verify", engine.management.events)

    def test_rootfs_drift_on_resume_blocks_before_another_delete(self):
        engine = make_engine()
        engine.management.migration_outcome = "unknown"
        with self.assertRaises(UnknownOutcome):
            engine.apply()
        before = copy.deepcopy(engine.kube.events)
        inspect = engine.management.inspect
        engine.management.inspect = lambda node: {
            **inspect(node), "runtime_rootfs_sha256": "b" * 64}
        with self.assertRaises(Blocked):
            engine.run()
        self.assertEqual([e for e in engine.kube.events[len(before):] if e[0] == "delete"], [])

    def test_committed_delete_without_replacement_resumes_readiness_wait(self):
        engine = make_engine()
        original_delete = engine.kube.delete_pod
        def delete_and_interrupt(pod):
            engine.kube.pods.remove(pod)
            engine.kube.events.append(("delete", pod["metadata"]["uid"]))
            raise SystemExit("DELETE committed before Waiting was saved")
        engine.kube.delete_pod = delete_and_interrupt
        with self.assertRaises(SystemExit):
            engine.apply()
        self.assertEqual(engine.state["nodes"][0]["phase"], "Deleting")
        inspect, sleep = engine.management.inspect, engine.sleep
        def inspect_live(node):
            if not any(p["spec"]["nodeName"] == node for p in engine.kube.pods):
                raise UnknownOutcome("replacement not ready")
            return inspect(node)
        def ready(seconds):
            sleep(seconds)
            if not any(p["spec"]["nodeName"] == "a" for p in engine.kube.pods):
                engine.kube.pods.append(engine.kube.pod("a", "a-old-new"))
        engine.management.inspect = inspect_live
        engine.sleep = ready
        engine.kube.delete_pod = original_delete
        self.assertEqual(engine.run()["phase"], "Completed")
        self.assertEqual([e for e in engine.kube.events if e == ("delete", "a-old")],
                         [("delete", "a-old")])


if __name__ == "__main__":
    unittest.main()
