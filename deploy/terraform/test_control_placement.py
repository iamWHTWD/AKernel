#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Render cloud values and control-pool defaults without providers or a cluster."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = os.environ.get("AKERNEL_TEST_TERRAFORM") or shutil.which("terraform")
CHART = ROOT / "deploy/akernel/charts/core"
CONTROLS = ("akernel-adx-redis", "akernel-adx-coordinator", "akernel-adx-ingress-api")


def render(values):
    result = subprocess.run(
        ["helm", "template", "akernel", str(CHART), "-f", "-"],
        input=yaml.safe_dump(values), text=True, capture_output=True, check=True,
    )
    return {
        item["metadata"]["name"]: item["spec"]["template"]["spec"]
        for item in yaml.safe_load_all(result.stdout)
        if item and item.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")
    }


class ControlPlacementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TERRAFORM or not shutil.which("helm"):
            raise RuntimeError("Placement tests require Terraform and Helm")

    def evaluate(self, expression, config=""):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "main.tf").write_text(config)
            result = subprocess.run(
                [TERRAFORM, "console", "-no-color"], cwd=directory,
                input="jsonencode(" + expression + ")\n",
                text=True, capture_output=True, check=True,
            )
        return json.loads(json.loads(result.stdout))

    def assert_placement(self, pods):
        for name in CONTROLS:
            with self.subTest(component=name):
                pod = pods[name]
                self.assertEqual(pod["nodeSelector"], {"node-role": "control"})
                self.assertIn(
                    dict(key="node-role", operator="Equal", value="control", effect="NoSchedule"),
                    pod["tolerations"],
                )
                self.assertNotIn("akernel.io/image-pull-ready", json.dumps(pod))
                # An empty control pool must leave these Pods pending, not
                # silently select a compute node or an unlabeled legacy node.
                for labels in ({"node-role": "compute"}, {}):
                    self.assertFalse(all(labels.get(k) == v for k, v in pod["nodeSelector"].items()))
        terms = pods["akernel-node"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        for term in terms:
            expression = next(item for item in term["matchExpressions"] if item["key"] == "node-role")
            self.assertEqual(expression["operator"], "NotIn")
            self.assertTrue({"control", "dragonfly-seed", "dragonfly-server"} <= set(expression["values"]))

    def test_default_chart_separates_control_and_compute(self):
        self.assert_placement(render({}))

    def test_existing_control_pool_selectors_and_taints_are_configurable(self):
        roles = ("redis", "coordinator", "ingressApi")
        settings = dict(
            nodeSelector={"node-role": "existing-control"},
            tolerations=[dict(key="dedicated", operator="Equal", value="control", effect="NoSchedule")],
        )
        pods = render({"adx": {role: settings for role in roles}})
        for name in CONTROLS:
            self.assertEqual(pods[name]["nodeSelector"], settings["nodeSelector"])
            self.assertEqual(pods[name]["tolerations"], settings["tolerations"])

    def test_both_cloud_templates_preserve_the_pool_boundary(self):
        for vendor in ("aliyun", "huaweicloud"):
            with self.subTest(vendor=vendor):
                module = ROOT / "deploy/terraform" / vendor
                source = (module / "main.tf").read_text()
                arguments = source.split("core_values = templatefile(", 1)[1].split("\n  })", 1)[0]
                values = {key: "1" for key in re.findall(r"^\s+(\w+)\s*=", arguments, re.M)}
                values.update(
                    acr_secret_enabled=False, adx_service_annotations={}, registry_auths={"auths": {}},
                    oss_auths={}, install_monitor=False, enable_kruise=False, enable_runc=False,
                    node_secret_create=False, node_home_use_csi_ephemeral=False,
                    schedule_placement_policy="spread", sandboxd_nat_backend="iptables",
                    adx_control_port=443, adx_data_port=80, adx_public_service_enabled=False,
                    adx_service_type="ClusterIP",
                )
                expression = "templatefile(" + json.dumps(str(module / "values-akernel.yaml.tmpl"))
                expression += ",jsondecode(" + json.dumps(json.dumps(values)) + "))"
                self.assert_placement(render(yaml.safe_load(self.evaluate(expression))))

    def test_control_pool_defaults_overrides_and_existing_pool_reuse(self):
        for vendor in ("aliyun", "huaweicloud"):
            module = ROOT / "deploy/terraform" / vendor
            main = (module / "main.tf").read_text()
            variable = re.search(r'(?ms)^variable "control_node_pool" \{.*?^\}', (module / "variables.tf").read_text())[0]
            pool = re.search(r"(?ms)^  control_pool = .*?^  \}\] : \[\]", main)[0]
            instance_field = "instance_types" if vendor == "aliyun" else "flavor_id"
            instance_value = ["ecs.example"] if vendor == "aliyun" else "example-flavor"
            for override in ({}, {"enabled": False}, {"name": "app-control", "size": 2, instance_field: instance_value}):
                with self.subTest(vendor=vendor, override=override):
                    config = variable + "\nlocals {\n" + pool + "\n}\n"
                    with tempfile.TemporaryDirectory() as directory:
                        (Path(directory) / "main.tf").write_text(config)
                        (Path(directory) / "terraform.tfvars.json").write_text(json.dumps({"control_node_pool": override}))
                        result = subprocess.run(
                            [TERRAFORM, "console", "-no-color"], cwd=directory,
                            input="jsonencode(local.control_pool)\n", text=True, capture_output=True, check=True,
                        )
                    pools = json.loads(json.loads(result.stdout))
                    if override.get("enabled") is False:
                        self.assertEqual(pools, [])
                        continue
                    self.assertEqual(len(pools), 1)
                    self.assertEqual(pools[0]["name"], override.get("name", "control"))
                    self.assertEqual(pools[0]["size"], override.get("size", 1))
                    self.assertEqual(pools[0]["labels"], {"node-role": "control"})
                    self.assertEqual(pools[0]["taints"], [dict(key="node-role", value="control", effect="NoSchedule")])
                    self.assertFalse(pools[0]["data_disk_enabled"])
                    self.assertEqual(pools[0][instance_field], override.get(instance_field))
                    if vendor == "aliyun":
                        self.assertFalse(pools[0]["use_akernel_data_disk"])
                        self.assertIsNone(pools[0]["pod_pids_limit"])
            self.assertIn("local.control_pool", main.split("all_extra_node_pools = ", 1)[1].splitlines()[0])
            release = main.split('resource "helm_release" "akernel_core"', 1)[1].split('\nresource "', 1)[0]
            resource = "alicloud_cs_kubernetes_node_pool.extra" if vendor == "aliyun" else "huaweicloud_cce_node_pool.extra"
            self.assertIn(resource, release.split("depends_on", 1)[1])


if __name__ == "__main__":
    unittest.main()
