"""SDK smoke tests run only after operator authorization of an isolated node."""

from __future__ import annotations

import importlib
import inspect
import secrets

from .errors import Blocked, UnknownOutcome, UpgradeError
from .engine import op_id


class SDKCanary:
    def __init__(self, management, *, runtime="runsc", cpu=1000, memory=1024, sdk=None):
        self.runtime, self.cpu, self.memory, self.sdk = runtime, cpu, memory, sdk
        self.management = management

    def preflight(self):
        try:
            if self.sdk is None:
                self.sdk = importlib.import_module("akernel_sdk")
            registry = importlib.import_module("akernel_sdk._backends.registry")
            selected = registry.selected_backend()
            if selected != "adx":
                return ["SDK ADX backend is required; packaged YuanRong SDK is incompatible"]
            if "node_id" not in inspect.signature(self.sdk.Sandbox).parameters:
                return ["SDK Sandbox(node_id=...) is unavailable"]
        except (ImportError, AttributeError):
            return ["install an operator-qualified AKernel SDK with ADX/node_id support"]
        return []

    def namespace(self):
        return importlib.import_module("akernel_sdk._backends.adx")._NAMESPACE

    def run(self, state, node_id, grant):
        if self.preflight():
            raise Blocked("SDK ADX/node_id prerequisite is unavailable")
        if (not all(grant.get(k) for k in ("environment_id", "name", "source_node_id"))
                or grant.get("ready") is not True):
            raise Blocked("task-scoped canary identity and basic node readiness are required")
        sandbox = None
        failure = None
        try:
            sandbox = self.sdk.Sandbox(node_id=grant["source_node_id"], name=grant["name"],
                                       runtime=self.runtime, cpu=self.cpu, memory=self.memory,
                                       idle_timeout=300)
            if sandbox.id != grant["environment_id"]:
                raise UpgradeError("SDK canary identity differs from task authorization")
            payload = "node-upgrade-" + secrets.token_hex(16)
            path = "/tmp/akernel-node-upgrade-canary"
            sandbox.files.write(path, payload)
            outcome = self.management.migrate(state, grant["source_node_id"],
                {"instanceID": sandbox.id, "status": "running"},
                op_id(state["task_id"], node_id, "canary-" + sandbox.id),
                target_node_id=node_id, checkpoint_storage_alias=grant["checkpoint_storage_alias"])
            if (outcome.get("outcome") != "completed" or outcome.get("node_id") != node_id
                    or outcome.get("state", "").lower() != "running"):
                raise UnknownOutcome("shared checkpoint canary recovery not confirmed on target")
            if sandbox.files.read(path) != payload:
                raise UpgradeError("SDK canary file roundtrip mismatch")
            result = sandbox.commands.run("cat /tmp/akernel-node-upgrade-canary")
            if result.exit_code != 0 or result.stdout.strip() != payload:
                raise UpgradeError("SDK canary command failed")
        except Exception as error:
            failure = error
        finally:
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception as error:
                    raise UnknownOutcome("SDK canary deletion unconfirmed; isolate and inspect") from error
        if failure is not None:
            # Constructor/transport failures can leave an accepted Environment.
            # Coordinator's task canary record is retained for inspection/cleanup.
            raise UnknownOutcome("SDK canary failed; inspect task-owned Environment before retry") from failure
