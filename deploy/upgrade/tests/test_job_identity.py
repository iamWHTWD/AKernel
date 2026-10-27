"""Bind the running Job to real Pod metadata without granting new permissions."""

import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manage import execution_identity
from akernel_upgrade.errors import UpgradeError


class JobIdentityTests(unittest.TestCase):
    def setUp(self):
        self.context = {"scripts_sha256": "a" * 64, "sdk_version": "9.0rc1"}
        self.image = "repo/node@sha256:" + "1" * 64
        self.pod = {"metadata": {"name": "upgrade-attempt", "uid": "attempt-uid",
                                 "namespace": "akernel"},
                    "spec": {"containers": [{"name": "executor", "image": self.image}]}}
        environment = {"AKERNEL_UPGRADE_JOB_CONTEXT": json.dumps(self.context),
                       "AKERNEL_UPGRADE_POD_NAME": "upgrade-attempt",
                       "AKERNEL_UPGRADE_POD_UID": "attempt-uid"}
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.calls = []
        case = self
        class Kube:
            namespace = "akernel"
            def get(self, kind, name):
                case.calls.append((kind, name))
                return copy.deepcopy(case.pod)
        self.kube = Kube()

    def test_image_is_read_from_current_pod_and_pod_uid_is_not_part_of_shared_identity(self):
        identity = execution_identity(self.kube)
        self.assertEqual(identity, {**self.context, "image": self.image})
        self.assertEqual(self.calls, [("pods", "upgrade-attempt")])
        self.pod["metadata"].update(name="resume-attempt", uid="new-uid")
        os.environ.update(AKERNEL_UPGRADE_POD_NAME="resume-attempt", AKERNEL_UPGRADE_POD_UID="new-uid")
        self.assertEqual(execution_identity(self.kube), identity)

    def test_missing_context_or_downward_identity_is_rejected(self):
        for name in ("AKERNEL_UPGRADE_JOB_CONTEXT", "AKERNEL_UPGRADE_POD_NAME", "AKERNEL_UPGRADE_POD_UID"):
            with self.subTest(name=name), patch.dict(os.environ):
                del os.environ[name]
                with self.assertRaises(UpgradeError):
                    execution_identity(self.kube)
        self.assertFalse(self.calls)

    def test_changed_terminating_or_wrong_namespace_pod_is_rejected(self):
        for changes in ({"uid": "other-uid"}, {"namespace": "other"},
                        {"deletionTimestamp": "now"}):
            original = copy.deepcopy(self.pod["metadata"])
            self.pod["metadata"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(UpgradeError):
                execution_identity(self.kube)
            self.pod["metadata"] = original

    def test_mutable_missing_or_duplicate_executor_image_is_rejected(self):
        for containers in ([{"name": "executor", "image": "repo/node:latest"}], [],
                           self.pod["spec"]["containers"] * 2):
            self.pod["spec"]["containers"] = containers
            with self.subTest(containers=containers), self.assertRaises(UpgradeError):
                execution_identity(self.kube)

    def test_invalid_bundle_context_is_rejected_before_any_api_call(self):
        for value in ([], {}, {**self.context, "scripts_sha256": "short"},
                      {**self.context, "sdk_version": ""}):
            os.environ["AKERNEL_UPGRADE_JOB_CONTEXT"] = json.dumps(value)
            with self.subTest(value=value), self.assertRaises(UpgradeError):
                execution_identity(self.kube)
        self.assertFalse(self.calls)

    def test_plain_local_invocation_has_no_job_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(execution_identity(self.kube))
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
