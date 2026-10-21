#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Render real Terraform and Helm templates offline. Both CLIs are required."""

import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml


ALIYUN = Path(__file__).resolve().parents[1]
ROOT = ALIYUN.parents[2]
TERRAFORM = os.environ.get('AKERNEL_TEST_TERRAFORM') or shutil.which('terraform')
if not TERRAFORM and (ROOT / '.akernel/tools/terraform').is_file():
    TERRAFORM = str(ROOT / '.akernel/tools/terraform')


class TerraformRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TERRAFORM or not shutil.which('helm'):
            raise RuntimeError('ACK rendering tests require Terraform and Helm; no checks may be skipped')

    def evaluate(self, expression):
        # No providers, backend, saved deployment state or cloud credentials.
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([TERRAFORM, 'console', '-no-color'], cwd=directory,
                                    input='jsonencode(' + expression + ')\n', text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return json.loads(json.loads(result.stdout))

    def test_readiness_affinity_only_in_auto_profiles(self):
        source = (ALIYUN / 'main.tf').read_text().split('core_values = templatefile(', 1)[1].split('\n  })', 1)[0]
        values = {key: '1' for key in re.findall(r'^\s+(\w+)\s*=', source, re.M)}
        values.update(acr_secret_enabled=False, adx_service_annotations={}, registry_auths={'auths': {}},
                      oss_auths={}, install_monitor=False, enable_kruise=False, enable_runc=False,
                      node_secret_create=False, node_home_use_csi_ephemeral=False,
                      schedule_placement_policy='spread', sandboxd_nat_backend='iptables',
                      adx_control_port=443, adx_data_port=80, adx_public_service_enabled=False,
                      adx_service_type='ClusterIP',
                      master_image_repository='registry.cn-hangzhou.aliyuncs.com/akernel/all-in-one', master_image_tag='control',
                      node_image_repository='registry.cn-hangzhou.aliyuncs.com/akernel/all-in-one', node_image_tag='worker')
        template = json.dumps(str(ALIYUN / 'values-akernel.yaml.tmpl'))
        control_placement = {}
        for auto in (False, True):
            with self.subTest(auto=auto):
                values['node_image_pull_auto'] = auto
                rendered = self.evaluate('templatefile(' + template + ', jsondecode(' + json.dumps(json.dumps(values)) + '))')
                self.assertEqual('akernel.io/image-pull-ready' in rendered, auto)
                self.assertIn('- dragonfly-seed', rendered)
                self.assertIn('- dragonfly-server', rendered)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'values.yaml'
                    path.write_text(rendered)
                    chart = subprocess.run(['helm', 'template', 'akernel-core', str(ROOT / 'deploy/akernel/charts/core'),
                                            '--values', str(path)],
                                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(chart.returncode, 0, chart.stderr)
                    resources = [item for item in yaml.safe_load_all(chart.stdout) if item]
                    for name in ('akernel-adx-redis', 'akernel-adx-coordinator', 'akernel-adx-ingress-api'):
                        pod = next(item for item in resources
                                   if item.get('kind') in ('Deployment', 'StatefulSet')
                                   and item['metadata']['name'] == name)['spec']['template']['spec']
                        self.assertNotIn('akernel.io/image-pull-ready', json.dumps(pod), name)
                        placement = {key: pod.get(key) for key in ('nodeSelector', 'affinity', 'tolerations')}
                        if not auto:
                            control_placement[name] = placement
                        else:
                            self.assertEqual(placement, control_placement[name], name)
                    worker = next(item for item in resources
                                  if item.get('kind') == 'DaemonSet'
                                  and item['metadata']['name'] == 'akernel-node')['spec']['template']['spec']
                    expressions = worker['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'][0]['matchExpressions']
                    self.assertEqual([item for item in expressions if item['key'] == 'akernel.io/image-pull-ready'],
                                     [dict(key='akernel.io/image-pull-ready', operator='In', values=['true'])] if auto else [])
                    self.assertNotIn('registry.example.com', chart.stdout)
                    images = re.findall(r'(?m)^\s+image: "([^"]+)"$', chart.stdout)
                    self.assertEqual(images.count(values['master_image_repository'] + ':control'), 3)
                    # The node's init container uses the same worker image.
                    self.assertEqual(images.count(values['node_image_repository'] + ':worker'), 2)

    def test_shared_acr_policy(self):
        policy = json.loads((ALIYUN / 'node-image-pull-policy.json').read_text())['repository_pattern']
        for repository in ('registry.cn-hangzhou.aliyuncs.com/akernel/all-in-one',
                           'registry-vpc.cn-hangzhou.aliyuncs.com/akernel/all-in-one',
                           'example-registry.ap-southeast-3.cr.aliyuncs.com/akernel/all-in-one',
                           'example.com/akernel/all-in-one', 'registry.cn-hangzhou.aliyuncs.com/repo:tag'):
            with self.subTest(repository=repository):
                expression = 'can(regex(' + json.dumps(policy) + ',' + json.dumps(repository) + '))'
                self.assertEqual(self.evaluate(expression), re.fullmatch(policy, repository) is not None)

    def test_core_dependency_isolation_and_matching_release_settings(self):
        source = (ALIYUN / 'main.tf').read_text()
        releases = dict(re.findall(r'resource "helm_release" "(akernel_core(?:_dragonfly)?)" \{(.*?)\n\}', source, re.S))
        direct, auto = releases['akernel_core'], releases['akernel_core_dragonfly']
        self.assertNotIn('helm_release.dragonfly', direct)
        self.assertIn('helm_release.dragonfly', auto)
        self.assertIn('count = local.node_image_pull_auto ? 0 : 1', direct)
        self.assertIn('count = local.node_image_pull_auto ? 1 : 0', auto)
        self.assertIn('from = helm_release.akernel_core\n  to   = helm_release.akernel_core[0]', source)

        def settings(body):
            # The separate resources are needed for static graph isolation;
            # their Helm settings must stay identical.
            return [re.sub(r'\s+=\s+', ' = ', line.strip()) for line in body.splitlines()
                    if line.strip() and not line.strip().startswith(('#', 'count ', 'depends_on '))]
        self.assertEqual(settings(direct), settings(auto))

    def test_full_user_data_payload_and_profile_checksum(self):
        profile = dict(cluster_id='c-offline', region='cn-hangzhou',
                       registry='registry.cn-hangzhou.aliyuncs.com', service_cidr='10.20.0.0/20',
                       domain='dragonfly-seed-client.dragonfly-system.svc.cluster.local',
                       helper_sha256=hashlib.sha256((ALIYUN / 'node-image-pull.py').read_bytes()).hexdigest())
        helper = json.dumps(str(ALIYUN / 'node-image-pull.py'))
        template = json.dumps(str(ALIYUN / 'node-image-pull.sh.tftpl'))
        variables = '{helper_gzip_b64=base64gzip(file(' + helper + ')),profile_b64=base64encode(' + json.dumps(json.dumps(profile)) + ')}'
        rendered = self.evaluate('templatefile(' + template + ',' + variables + ')')
        self.assertIn('systemctl start --no-block', rendered)
        encoded = re.findall(r"printf '%s' '([^']+)'", rendered)
        self.assertEqual(gzip.decompress(base64.b64decode(encoded[0])), (ALIYUN / 'node-image-pull.py').read_bytes())
        self.assertEqual(json.loads(base64.b64decode(encoded[1])), profile)
        base = self.evaluate('templatefile(' + json.dumps(str(ALIYUN.parent / 'shared/node-bootstrap.sh.tftpl')) +
                             ',{sandboxd_nat_backend="iptables",max_user_namespaces=65536})')
        pids = self.evaluate('templatefile(' + json.dumps(str(ALIYUN.parent / 'shared/node-pid-budget.sh.tftpl')) + ',{})')
        complete = '\n'.join([base, pids, rendered])
        # Conservative envelope, including the original module/PID bootstrap.
        self.assertLess(len(complete.encode()), 16384)
        subprocess.run(['bash', '-n'], input=complete, text=True, check=True)


if __name__ == '__main__':
    unittest.main()
