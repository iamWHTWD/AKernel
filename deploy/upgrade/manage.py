#!/usr/bin/env python3
"""Operator entry point. Omitting a command always produces a read-only plan."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
from pathlib import Path

from akernel_upgrade.canary import SDKCanary
from akernel_upgrade.clients import HTTP, Inventory, Kubernetes
from akernel_upgrade.engine import Executor, Settings
from akernel_upgrade.errors import UpgradeError
from akernel_upgrade.protocol import Management, Scaling


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("command", nargs="?", default="plan",
                       choices=("plan", "apply", "status", "resume", "abort"))
    value.add_argument("--config", required=True, help="JSON configuration; credentials stay in files")
    value.add_argument("--execute", action="store_true", help="required for every mutating command")
    value.add_argument("--kubeconfig")
    return value


def status_report(runner, management, kube, scaler_name, task_id):
    """Observe independent authorities; an unavailable read stays unknown."""
    def observe(read):
        try:
            value = read()
            if not isinstance(value, dict):
                raise ValueError("status object required")
            return {"outcome": "observed", "value": value}
        except (UpgradeError, OSError, ValueError, KeyError, TypeError) as error:
            # Only expose classification: transport causes/config paths can
            # contain credentials. No failed channel is projected as Ready.
            return {"outcome": "unknown", "error_kind": type(error).__name__}

    return {"local": observe(lambda: runner.load(check_executor=False)),
            "adx": observe(lambda: management.status({"task_id": task_id})),
            "scaler": observe(lambda: json.loads(
                kube.get("configmaps", scaler_name)["data"]["status.json"]))}


def execution_identity(kube):
    """Bind a Job attempt to its actual script bundle, SDK and Pod image."""
    raw = os.environ.get("AKERNEL_UPGRADE_JOB_CONTEXT")
    pod_name = os.environ.get("AKERNEL_UPGRADE_POD_NAME")
    pod_uid = os.environ.get("AKERNEL_UPGRADE_POD_UID")
    if raw is None and not pod_name and not pod_uid:
        return None  # Source-run local executors keep their existing contract.
    if not raw or not pod_name or not pod_uid:
        raise UpgradeError("Job context and Downward API Pod name/UID are required")
    context = json.loads(raw)
    if (not isinstance(context, dict) or set(context) != {"scripts_sha256", "sdk_version"}
            or not isinstance(context["scripts_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", context["scripts_sha256"])
            or not isinstance(context["sdk_version"], str) or not context["sdk_version"]):
        raise UpgradeError("invalid Job execution context")
    pod = kube.get("pods", pod_name)
    metadata = pod["metadata"]
    if (metadata["uid"] != pod_uid or metadata["namespace"] != kube.namespace
            or metadata.get("deletionTimestamp")):
        raise UpgradeError("executor Pod identity changed or is terminating")
    containers = [item for item in pod["spec"]["containers"] if item["name"] == "executor"]
    if (len(containers) != 1 or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}",
                                               containers[0]["image"])):
        raise UpgradeError("executor Pod image must use an immutable @sha256 digest")
    return {**context, "image": containers[0]["image"]}


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command in ("apply", "resume", "abort") and not args.execute:
        parser().error("mutating commands require --execute")
    try:
        config = json.loads(Path(args.config).read_text())
        settings = Settings(**config["upgrade"])
        http = HTTP(**config["adx"])
        kube = Kubernetes(settings.namespace, args.kubeconfig)
        identity = execution_identity(kube)
        scaling = Scaling(kube, **config["scaler"])
        token = Path(config["adx"]["token_path"]).read_text().strip()
        if not token or "\n" in token or "\r" in token:
            raise UpgradeError("invalid SDK token file")
        os.environ["AKERNEL_TOKEN"] = token
        os.environ["AKERNEL_SERVER_ADDRESS"] = config["sdk_server_address"]
        os.environ["AKERNEL_VERIFY_TLS"] = "true"
        if config["adx"].get("ca_file"):
            os.environ["SSL_CERT_FILE"] = config["adx"]["ca_file"]
        management = Management(http)
        canary = SDKCanary(management, cpu=settings.canary_cpu, memory=settings.canary_memory)
        runner = Executor(settings, kube, Inventory(http), management, canary, scaling,
                          execution_identity=identity)
        if args.command == "plan":
            result = runner.plan()
        elif args.command == "status":
            result = status_report(runner, management, kube, config["scaler"]["name"], settings.task_id)
        elif args.command == "apply":
            result = runner.apply()
        elif args.command == "resume":
            runner.load()
            result = runner.run()
        else:
            result = runner.abort()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (UpgradeError, OSError, ValueError, KeyError, TypeError) as error:
        # Transport errors never include response bodies, bearer values or
        # config dumps. State carries identifiers and stages only.
        print(f"upgrade stopped: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Preserve journaled intent and stop; SIGTERM must work for container PID 1.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    raise SystemExit(main())
