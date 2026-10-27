"""DELETE intent recovery against a stateful, rejecting management contract."""

import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_upgrade as fixtures
from akernel_upgrade.errors import Conflict, UnknownOutcome
from akernel_upgrade.protocol import Management


class DeleteManagement(fixtures.FakeManagement):
    """Model the server's UID/session binding and outstanding DELETE RV."""

    def __init__(self, kube, inventory):
        super().__init__(kube, inventory)
        self.bindings = {}
        self.pending = {}
        self.confirm_count = 0
        self.lose_confirm = set()
        self.lose_revoke_reply = False
        self.revocations = []

    def begin(self, state):
        result = super().begin(state)
        for node in state["nodes"]:
            self.bindings.setdefault(node["node_id"], node["old_uid"])
        return result

    def confirm_delete(self, state, node, proof, pod):
        uid, rv = pod["metadata"]["uid"], pod["metadata"]["resourceVersion"]
        if uid != self.bindings[node] or proof["pod_uid"] != uid:
            raise Conflict("delete permit belongs to another Pod")
        self.confirm_count += 1
        self.pending[node] = {"uid": uid, "rv": rv, "session": "session-" + node}
        self.events.append("confirm-delete")
        if self.confirm_count in self.lose_confirm:
            raise UnknownOutcome("delete permit persisted; reply lost")

    def rollback(self, state, node, pod):
        uid = pod["metadata"]["uid"]
        if uid != self.bindings[node]:
            # A committed original DELETE changes the exact Pod binding. Its
            # old RV authority cannot transfer to the replacement.
            if node not in self.pending:
                raise Conflict("replacement requires the original delete permit")
            self.bindings[node] = uid
            self.pending.pop(node)
        return self.quiesce(state, node)

    def revoke_delete(self, state, node, pod, expected_delete_resource_version=None):
        uid, rv = pod["metadata"]["uid"], pod["metadata"]["resourceVersion"]
        if uid != self.bindings[node] or not expected_delete_resource_version:
            raise Conflict("exact Pod and original DELETE RV are required")
        pending = self.pending.get(node)
        if pending is not None:
            if (pending["uid"] != uid or pending["rv"] != expected_delete_resource_version
                    or pending["session"] != "session-" + node):
                raise Conflict("a different DELETE intent is current")
            if rv == expected_delete_resource_version:
                raise Conflict("old DELETE RV has not been invalidated")
            self.pending.pop(node)
        # Replaying a confirmed revocation is safe only in the same binding;
        # no scheduling or execution authority is restored by this call.
        self.revocations.append((uid, expected_delete_resource_version, rv))
        if self.lose_revoke_reply:
            self.lose_revoke_reply = False
            raise UnknownOutcome("revocation persisted; reply lost")

    def restore_original(self, state, node, pod):
        source = next(n for n in state["nodes"] if n["node_id"] == node)
        if node in self.pending or pod["metadata"]["uid"] != source["old_uid"]:
            raise Conflict("original Pod still has an outstanding DELETE")
        return {"restored_original": True, "node_id": node, "pod_uid": source["old_uid"],
                "node_session_id": source["node_session_id"]}


class DeleteRecoveryTests(unittest.TestCase):
    def fixture(self):
        case = fixtures.PhaseRecoveryTests().fixture()
        case.manager = DeleteManagement(case.kube, case.inventory)
        case.engine.management = case.manager
        return case

    @staticmethod
    def resumed(case):
        return fixtures.PhaseRecoveryTests.resumed(case)

    def stopped_after_forward_confirmation(self):
        case = self.fixture()
        case.manager.lose_confirm.add(1)
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        self.assertIn("a", case.manager.pending)
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))
        return case

    def stopped_with_saved_delete_intent(self):
        case = self.stopped_after_forward_confirmation()
        # Isolate revoke recovery from the separate confirm-before-save bug.
        # This is the durable intent left by an accepted-but-unknown DELETE.
        node = case.engine.state["nodes"][0]
        node["delete_intent"] = {"uid": "a-old", "resourceVersion": "1"}
        case.engine.save()
        return case

    def test_forward_confirm_reply_loss_can_abort_without_deleting_original(self):
        case = self.stopped_after_forward_confirmation()
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual({p["metadata"]["uid"] for p in case.kube.pods}, {"a-old", "b-old"})
        self.assertFalse(case.manager.pending)
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))
        self.assertFalse(case.scaling.protected)

    def test_forward_intent_save_failure_prevents_delete_confirmation(self):
        case = self.fixture()
        save = case.kube.save_state

        def fail_before_intent_persists(obj, state):
            if any(node.get("delete_intent") for node in state["nodes"]):
                raise SystemExit("stopped before intent save")
            return save(obj, state)

        case.kube.save_state = fail_before_intent_persists
        with self.assertRaises(SystemExit):
            case.engine.apply()
        self.assertEqual(case.manager.confirm_count, 0)
        self.assertFalse(case.manager.pending)
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))

    def test_rollback_confirm_reply_loss_retries_only_the_failed_replacement(self):
        case = self.fixture()
        case.probe.error = UnknownOutcome("new image probe failed")
        with self.assertRaises(UnknownOutcome):
            case.engine.apply()
        case.probe.error = None
        case.manager.lose_confirm.add(2)
        with self.assertRaises(UnknownOutcome):
            case.engine.abort()
        persisted = self.resumed(case)
        persisted.load()
        node = persisted.state["nodes"][0]
        self.assertEqual(node.get("rollback_phase"), "Deleting")
        self.assertEqual(node.get("rollback_delete_intent"), {"uid": "a-old-new", "resourceVersion": "1"})
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual([e[1] for e in case.kube.events if e[0] == "delete"],
                         ["a-old", "a-old-new"])
        self.assertEqual({p["metadata"]["uid"] for p in case.kube.pods},
                         {"a-old-new-new", "b-old"})

    def test_revoke_reply_loss_replays_original_delete_rv(self):
        case = self.stopped_with_saved_delete_intent()
        case.manager.lose_revoke_reply = True
        with self.assertRaises(UnknownOutcome):
            self.resumed(case).abort()
        self.assertNotIn("a", case.manager.pending)
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual([r[:2] for r in case.manager.revocations], [("a-old", "1"), ("a-old", "1")])
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))

    def test_revoke_before_local_clear_save_crash_is_idempotent(self):
        case = self.stopped_with_saved_delete_intent()
        save = case.kube.save_state
        crashed = False

        def lose_local_clear(obj, state):
            nonlocal crashed
            if (case.manager.revocations and not crashed
                    and not state["nodes"][0].get("delete_intent")):
                crashed = True
                raise SystemExit("revoked, but intent clear was not saved")
            return save(obj, state)

        case.kube.save_state = lose_local_clear
        with self.assertRaises(SystemExit):
            self.resumed(case).abort()
        case.kube.save_state = save
        self.assertNotIn("a", case.manager.pending)
        self.assertEqual(self.resumed(case).abort()["phase"], "Aborted")
        self.assertEqual([r[:2] for r in case.manager.revocations], [("a-old", "1"), ("a-old", "1")])
        self.assertFalse(any(e[0] == "delete" for e in case.kube.events))

    def test_revoke_transport_sends_original_expected_delete_rv(self):
        calls = []

        class HTTP:
            def request(self, method, path, body=None):
                if method == "GET":
                    return {"node_session_id": "session-a"}
                calls.append(copy.deepcopy(body))
                return {}

        state = {"task_id": "task", "epoch": 1, "phase": "Aborting", "nodes": [
            {"node_id": "a", "old_uid": "a-old", "node_session_id": "session-a"}]}
        Management(HTTP()).revoke_delete(state, "a", {
            "metadata": {"uid": "a-old", "resourceVersion": "3"}}, "1")
        self.assertEqual(calls[0]["expected_delete_resource_version"], "1")
        self.assertEqual(calls[0]["pod_resource_version"], "3")
        self.assertEqual(calls[0]["pod_uid"], "a-old")
        self.assertEqual(calls[0]["node_session_id"], "session-a")


if __name__ == "__main__":
    unittest.main()
