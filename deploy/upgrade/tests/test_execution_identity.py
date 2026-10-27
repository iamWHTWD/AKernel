"""An interrupted task must retain its original executor environment."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_scaler_handshake import make_engine
from akernel_upgrade.engine import Executor
from akernel_upgrade.errors import Conflict, UnknownOutcome


IDENTITY = {"sdk_version": "0.2.0rc3", "scripts_sha256": "1" * 64,
            "image": "repo/node@sha256:" + "2" * 64}


def restarted(engine, identity):
    return Executor(engine.cfg, engine.kube, engine.inventory, engine.management,
                    engine.canary, engine.scaling, execution_identity=identity,
                    clock=engine.clock, sleep=engine.sleep)


class ExecutionIdentityTests(unittest.TestCase):
    def interrupted(self, identity=IDENTITY):
        engine = restarted(make_engine(), identity)
        migrate = engine.management.migrate

        def lost_reply(*args, **kwargs):
            migrate(*args, **kwargs)
            raise UnknownOutcome("completed migration reply lost")

        engine.management.migrate = lost_reply
        with self.assertRaises(UnknownOutcome):
            engine.apply()
        engine.management.migrate = migrate
        return engine

    def test_apply_persists_an_independent_copy_of_execution_identity(self):
        identity = copy.deepcopy(IDENTITY)
        engine = restarted(make_engine(), identity)
        identity["sdk_version"] = "different"
        begin = engine.management.begin

        def check_persisted(state):
            saved = json.loads(engine.kube.maps[engine.state_name("ds")]["data"]["state.json"])
            self.assertEqual(saved["configuration"]["execution_identity"], IDENTITY)
            return begin(state)

        engine.management.begin = check_persisted
        self.assertEqual(engine.apply()["phase"], "Completed")
        self.assertEqual(engine.execution_identity, IDENTITY)

    def test_changed_or_missing_environment_blocks_resume_and_abort_before_writes(self):
        changes = ({"sdk_version": "0.2.0rc4"}, {"scripts_sha256": "3" * 64},
                   {"image": "repo/node@sha256:" + "4" * 64})
        identities = [{**copy.deepcopy(IDENTITY), **change} for change in changes]
        identities.extend((None, {}))
        for operation in ("run", "abort"):
            for identity in identities:
                with self.subTest(operation=operation, identity=identity):
                    engine = self.interrupted()
                    before = copy.deepcopy((engine.kube.events, engine.management.events,
                                            engine.scaling.__dict__, engine.canary.runs))
                    resumed = restarted(engine, identity)
                    with self.assertRaises(Conflict):
                        getattr(resumed, operation)()
                    self.assertEqual((engine.kube.events, engine.management.events,
                                      engine.scaling.__dict__, engine.canary.runs), before)

    def test_same_environment_resumes_without_replaying_completed_migration(self):
        engine = self.interrupted()
        operation = engine.state["nodes"][0]["migrations"]["work"]
        self.assertFalse(operation["completed"])
        self.assertEqual(engine.management.events.count("migrate"), 1)
        resumed = restarted(engine, copy.deepcopy(IDENTITY))
        self.assertEqual(resumed.run()["phase"], "Completed")
        self.assertTrue(resumed.state["nodes"][0]["migrations"]["work"]["completed"])
        self.assertEqual(engine.management.events.count("migrate"), 1)

    def test_legacy_local_task_can_continue_without_an_execution_identity(self):
        engine = self.interrupted(identity=None)
        self.assertNotIn("execution_identity", engine.state["configuration"])
        self.assertEqual(restarted(engine, None).run()["phase"], "Completed")

    def test_read_only_status_can_inspect_a_task_from_a_different_environment(self):
        engine = self.interrupted()
        resumed = restarted(engine, None)
        before = copy.deepcopy(engine.kube.events)
        self.assertEqual(resumed.load(check_executor=False)["phase"], "NeedsAttention")
        self.assertEqual(engine.kube.events, before)


if __name__ == "__main__":
    unittest.main()
