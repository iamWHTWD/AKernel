"""Persisted intent recovery against authoritative task receipts."""

import copy
from dataclasses import replace
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_scaler_handshake import make_engine
from akernel_upgrade.engine import Executor
from akernel_upgrade.errors import Conflict, UnknownOutcome
from akernel_upgrade.protocol import Management


class MigrationRecoveryTests(unittest.TestCase):
    def interrupted(self):
        engine = make_engine()
        engine.inventory.records[0].update(generation=7, revision=11)
        migrate = engine.management.migrate
        def unavailable(*args, **kwargs):
            raise UnknownOutcome("inspect failed before migration POST")
        engine.management.migrate = unavailable
        with self.assertRaises(UnknownOutcome):
            engine.apply()
        engine.management.migrate = migrate
        node = engine.state["nodes"][0]
        operation = node["migrations"]["work"]
        task = {key: engine.state[key] for key in
                ("task_id", "epoch", "namespace", "daemonset_uid")}
        task.update(target_image=engine.state["image"], phase="Active", operations={}, nodes={
            "a": {"pod_uid": "a-old", "node_session_id": "session-a", "phase": "evacuating"}})
        engine.management.status = lambda _: copy.deepcopy(task)
        return engine, node, operation, task

    def receipt(self, operation, **changes):
        return {"environment_id": "work", "source_node_id": "a", "target_node_id": "b",
                "source_generation": 7, "source_revision": 11,
                "source_session_id": "session-a", "target_session_id": "session-b",
                "was_running": True, "phase": "Completed", **changes}

    def test_resume_configuration_drift_is_rejected_before_any_write(self):
        for change in ({"image": "repo/node@sha256:" + "3" * 64},
                       {"checkpoint_storage_alias": "another-shared"},
                       {"pool_id": "different-pool"}, {"container": "another-container"}):
            engine, _, _, _ = self.interrupted()
            resumed = Executor(replace(engine.cfg, **change), engine.kube, engine.inventory,
                               engine.management, engine.canary, engine.scaling)
            before = copy.deepcopy(engine.kube.events), copy.deepcopy(engine.management.events)
            with self.subTest(change=change), self.assertRaises(Conflict):
                resumed.run()
            self.assertEqual((engine.kube.events, engine.management.events), before)

    def test_scaler_identity_drift_cannot_redirect_task_protection(self):
        engine, _, _, _ = self.interrupted()
        engine.scaling.configuration = lambda: {"name": "other", "cluster_id": "other-cluster",
                                                 "pool_id": "other-pool", "count": 2}
        before = copy.deepcopy(engine.kube.events)
        resumed = Executor(engine.cfg, engine.kube, engine.inventory, engine.management,
                           engine.canary, engine.scaling)
        with self.assertRaises(Conflict):
            resumed.abort()
        self.assertEqual(engine.kube.events, before)

    def test_absent_operation_resends_same_id_only_with_unchanged_source(self):
        engine, node, operation, task = self.interrupted()
        calls = []
        def accepted(state, source, record, identity, **kwargs):
            calls.append((identity, kwargs))
            task["operations"][identity] = self.receipt(operation)
            engine.inventory.records = []
            return {"outcome": "completed", "node_id": "b", "state": "running"}
        engine.management.migrate = accepted
        engine.upgrade_node(node)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (operation["operation_id"], {
            "expected_source_generation": 7, "expected_source_revision": 11}))
        self.assertTrue(operation["completed"])

    def test_absent_operation_cannot_replay_after_source_or_task_changed(self):
        for key, value in (("generation", 8), ("revision", 12), ("node_id", "b")):
            engine, node, _, _ = self.interrupted()
            engine.inventory.records[0][key] = value
            engine.management.migrate = lambda *a, **kw: self.fail("changed source was replayed")
            with self.subTest(field=key), self.assertRaises(UnknownOutcome):
                engine.upgrade_node(node)
        engine, node, _, task = self.interrupted()
        task["epoch"] += 1
        engine.management.migrate = lambda *a, **kw: self.fail("wrong task was replayed")
        with self.assertRaises(UnknownOutcome):
            engine.upgrade_node(node)

    def test_completed_receipt_survives_later_pause_or_deletion(self):
        for records in ([], [{"instanceID": "work", "node_id": "b", "status": "paused",
                              "generation": 8, "revision": 15}]):
            engine, node, operation, task = self.interrupted()
            task["operations"][operation["operation_id"]] = self.receipt(operation)
            engine.inventory.records = records
            engine.management.migrate = lambda *a, **kw: self.fail("Completed was replayed")
            with self.subTest(records=records):
                engine.upgrade_node(node)
            self.assertTrue(operation["completed"])

    def test_completed_receipt_requires_matching_generation_and_identity(self):
        for changes in ({"source_generation": 8}, {"environment_id": "other"},
                        {"source_node_id": "other"}, {"target_node_id": "a"},
                        {"was_running": None}, {"phase": "Failed"}):
            engine, node, operation, task = self.interrupted()
            task["operations"][operation["operation_id"]] = self.receipt(operation, **changes)
            engine.inventory.records = []
            with self.subTest(changes=changes), self.assertRaises(UnknownOutcome):
                engine.upgrade_node(node)
            self.assertFalse(operation["completed"])

    def test_server_accepted_pause_state_replaces_stale_running_snapshot(self):
        engine, node, operation, task = self.interrupted()
        task["operations"][operation["operation_id"]] = self.receipt(
            operation, was_running=False, source_revision=12)
        engine.inventory.records = []
        engine.upgrade_node(node)
        self.assertTrue(operation["completed"])
        self.assertEqual(operation["expected_state"], "paused")

    def test_initial_request_fences_generation_and_accepts_concurrent_pause(self):
        engine = make_engine()
        engine.inventory.records[0].update(generation=7, revision=11)
        migrate = engine.management.migrate
        calls = []
        def accepted(state, source, record, identity, **kwargs):
            calls.append(kwargs)
            result = migrate(state, source, record, identity, **kwargs)
            engine.management.task["operations"][identity].update(was_running=False, source_revision=12)
            return {**result, "state": "paused"}
        engine.management.migrate = accepted
        self.assertEqual(engine.apply()["phase"], "Completed")
        self.assertEqual(calls, [{"expected_source_generation": 7}])
        operation = engine.state["nodes"][0]["migrations"]["work"]
        self.assertEqual(operation["expected_state"], "paused")

    def test_http_migration_preserves_both_replay_preconditions(self):
        calls = []
        class HTTP:
            def request(self, method, path, body=None):
                if method == "POST":
                    calls.append(body)
                return {"node_session_id": "session-a"}
        management = Management(HTTP())
        management.migrate({"task_id": "task", "epoch": 1,
                            "nodes": [{"node_id": "a", "old_uid": "pod-a"}]},
                           "a", {"instanceID": "work"}, "same-operation",
                           expected_source_generation=7, expected_source_revision=11)
        self.assertEqual(calls[0]["operation_id"], "same-operation")
        self.assertEqual(calls[0]["expected_source_generation"], 7)
        self.assertEqual(calls[0]["expected_source_revision"], 11)

    def test_abort_cancels_unaccepted_intent_only_after_authoritative_freeze(self):
        engine, node, operation, task = self.interrupted()
        freeze = engine.management.freeze
        def frozen(state):
            freeze(state)
            task["phase"] = "Aborting"
        engine.management.freeze = frozen
        self.assertEqual(engine.abort()["phase"], "Aborted")
        operation = engine.state["nodes"][0]["migrations"]["work"]
        self.assertTrue(operation["cancelled"])
        self.assertFalse(operation["completed"])
        self.assertFalse(any(event[0] == "delete" for event in engine.kube.events))

    def test_abort_does_not_cancel_a_stale_or_unfrozen_absence(self):
        engine, _, operation, _ = self.interrupted()
        with self.assertRaises(UnknownOutcome):
            engine.abort()
        self.assertNotIn("cancelled", operation)

    def test_frozen_absence_can_cancel_after_user_pause_or_delete(self):
        for records in ([], [{"instanceID": "work", "node_id": "a", "status": "paused",
                              "generation": 7, "revision": 15}]):
            engine, _, _, task = self.interrupted()
            engine.inventory.records = records
            engine.management.freeze = lambda _: task.update(phase="Aborting")
            with self.subTest(records=records):
                self.assertEqual(engine.abort()["phase"], "Aborted")
            operation = engine.state["nodes"][0]["migrations"]["work"]
            self.assertTrue(operation["cancelled"])
            self.assertFalse(operation["completed"])

    def test_abort_accepts_authoritative_cancelled_without_marking_migrated(self):
        engine, _, operation, task = self.interrupted()
        task["operations"][operation["operation_id"]] = self.receipt(operation, phase="Cancelled")
        engine.inventory.records[0]["revision"] += 1  # Failed Pause returned to Running.
        engine.management.freeze = lambda _: task.update(phase="Aborting")
        self.assertEqual(engine.abort()["phase"], "Aborted")
        operation = engine.state["nodes"][0]["migrations"]["work"]
        self.assertTrue(operation["cancelled"])
        self.assertFalse(operation["completed"])
        self.assertEqual(engine.inventory.records[0]["node_id"], "a")
        self.assertFalse(any(event[0] == "delete" for event in engine.kube.events))

    def test_abort_cancelled_receipt_still_requires_exact_historical_identity(self):
        for changes in ({"source_generation": 8}, {"environment_id": "other"},
                        {"source_node_id": "other"}, {"source_session_id": "other"}):
            engine, _, operation, task = self.interrupted()
            task["operations"][operation["operation_id"]] = self.receipt(
                operation, phase="Cancelled", **changes)
            engine.management.freeze = lambda _: task.update(phase="Aborting")
            with self.subTest(changes=changes), self.assertRaises(UnknownOutcome):
                engine.abort()
            self.assertNotIn("cancelled", engine.state["nodes"][0]["migrations"]["work"])

    def test_forward_resume_rejects_cancelled_without_sending_migration(self):
        engine, node, operation, task = self.interrupted()
        task["operations"][operation["operation_id"]] = self.receipt(operation, phase="Cancelled")
        engine.management.migrate = lambda *args, **kwargs: self.fail("Cancelled was replayed")
        with self.assertRaises(UnknownOutcome):
            engine.upgrade_node(node)
        self.assertFalse(operation["completed"])
        self.assertNotIn("cancelled", operation)
        self.assertFalse(any(event[0] == "delete" for event in engine.kube.events))

    def test_success_response_cannot_turn_cancelled_into_migrated(self):
        engine, node, operation, task = self.interrupted()
        task["operations"][operation["operation_id"]] = self.receipt(operation, phase="Cancelled")
        with self.assertRaises(UnknownOutcome):
            engine.complete_migration(node, "work", operation,
                                      {"outcome": "completed", "node_id": "b", "state": "running"})
        self.assertFalse(operation["completed"])

    def test_lost_verify_reply_retries_scheduling_completion_without_reprobing(self):
        engine = make_engine()
        verify = engine.management.verify
        calls = []
        def lost(*args):
            calls.append(args[1])
            verify(*args)
            if len(calls) == 1:
                raise UnknownOutcome("verified persisted before scheduling restoration")
        engine.management.verify = lost
        with self.assertRaises(UnknownOutcome):
            engine.apply()
        engine.run()
        self.assertEqual(calls.count("a"), 2)
        self.assertEqual(engine.canary.runs, 2)


if __name__ == "__main__":
    unittest.main()
