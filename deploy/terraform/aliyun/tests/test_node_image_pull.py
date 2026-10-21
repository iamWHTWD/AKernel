#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Offline ACK bootstrap tests. Never execute host commands or contact a cluster."""

import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock


ALIYUN = Path(__file__).resolve().parents[1]
ROOT = ALIYUN.parents[2]
spec = importlib.util.spec_from_file_location('node_image_pull', ALIYUN / 'node-image-pull.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
REGISTRY = 'registry.cn-hangzhou.aliyuncs.com'
DOMAIN = 'dragonfly-seed-client.dragonfly-system.svc.cluster.local'


class ConfigurationTest(unittest.TestCase):
    def test_native_python36(self):
        ast.parse((ALIYUN / 'node-image-pull.py').read_text(), feature_version=(3, 6))

    def test_real_cri_hosts_sections_and_fallback(self):
        for plugin in ('io.containerd.grpc.v1.cri', 'io.containerd.cri.v1.images'):
            for quote in ('"', "'"):
                text = '[plugins.' + quote + plugin + quote + ']\n use_local_image_pull=true\n'
                text += '[plugins.' + quote + plugin + quote + '.registry]\n config_path="/etc/containerd/cert.d:/etc/containerd/certs.d"\n'
                self.assertEqual(bootstrap.hosts_roots(text), [Path('/etc/containerd/cert.d'), Path('/etc/containerd/certs.d')])
        with self.assertRaisesRegex(RuntimeError, 'config_path'):
            bootstrap.hosts_roots('[plugins.other]\nconfig_path="/etc/containerd/certs.d"\n')
        text = bootstrap.mirror_config(REGISTRY, DOMAIN).decode()
        self.assertIn('server = "https://' + REGISTRY + '"', text)
        self.assertIn('capabilities = ["pull"]', text)
        self.assertNotIn('skip_verify', text)
        self.assertNotIn('Authorization', text)
        self.assertNotIn('resolve', text)
        for registry in ('../other', 'registry.cn-hangzhou.aliyuncs.com/escape', 'host:443', 'host"escape'):
            with self.assertRaises(RuntimeError):
                bootstrap.mirror_config(registry, DOMAIN)

    def test_transfer_service_rejects_multi_root_without_explicit_local_pull(self):
        plugin = '[plugins."io.containerd.cri.v1.images"]\n'
        registry = '[plugins."io.containerd.cri.v1.images".registry]\n'
        for flag in ('', 'use_local_image_pull=false\n'):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(RuntimeError, 'Transfer Service'):
                    bootstrap.hosts_roots(plugin + flag + registry + 'config_path="/etc/containerd/cert.d:/etc/containerd/certs.d"\n')
                self.assertEqual(bootstrap.hosts_roots(plugin + flag + registry + 'config_path="/etc/containerd/certs.d"\n'),
                                 [Path('/etc/containerd/certs.d')])
        # An unrelated plugin's local-pull flag cannot authorize the CRI path.
        with self.assertRaisesRegex(RuntimeError, 'Transfer Service'):
            bootstrap.hosts_roots('[plugins.other]\nuse_local_image_pull=true\n' + registry +
                                  'config_path="/etc/containerd/cert.d:/etc/containerd/certs.d"\n')

    def test_kubelet_dns_variants_and_loop_rejection(self):
        for text in ('clusterDNS:\n- 10.20.0.10\nresolvConf: /etc/resolv.conf\n',
                     'clusterDNS:\n  - "10.20.0.10"\nresolvConf: /etc/resolv.conf\n',
                     'clusterDNS: ["10.20.0.10"]\nresolvConf: /etc/resolv.conf\n',
                     '{"clusterDNS":["10.20.0.10"],"resolvConf":"/etc/resolv.conf"}'):
            self.assertEqual(bootstrap.kubelet_dns(text), (['10.20.0.10'], '/etc/resolv.conf'))
        for text in ('nameserver 127.0.0.53\n', 'nameserver 10.20.0.10\n', ''):
            with self.assertRaises(RuntimeError):
                bootstrap.upstream_addresses(text, ['10.20.0.10'])

    def test_customized_args_preserve_flags_and_are_idempotent(self):
        for original in ('', '# keep\nexport KUBELET_CUSTOMIZED_ARGS="--max-pods=120 --resolv-conf /old --v=2"\n'):
            changed = bootstrap.customized_args(original)
            self.assertEqual(changed, bootstrap.customized_args(changed.decode()))
            if original:
                self.assertIn(b'--max-pods=120', changed)
                self.assertIn(b'--v=2', changed)
                self.assertIn(b'# keep', changed)
            self.assertNotIn(b'/old', changed)
        for original in ('KUBELET_CUSTOMIZED_ARGS="$OTHER"\n',
                         'KUBELET_CUSTOMIZED_ARGS=a\nKUBELET_CUSTOMIZED_ARGS=b\n'):
            with self.assertRaises(RuntimeError):
                bootstrap.customized_args(original)

    def test_refuse_foreign_files_and_symlink_ancestors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hosts.toml'
            path.write_text('operator mirror')
            with self.assertRaises(RuntimeError):
                bootstrap.owned_write(path, b'new')
            self.assertEqual(path.read_text(), 'operator mirror')
            (Path(directory) / 'link').symlink_to(directory)
            with self.assertRaises(RuntimeError):
                bootstrap.atomic_write(Path(directory) / 'link' / 'other', b'new')


class WorkerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.base = self.root / 'bootstrap'
        self.base.mkdir(mode=0o700)
        for name, path in dict(BASE=self.base, CUSTOM=self.root / 'custom-args',
                               UPSTREAM=self.base / 'kubelet-resolv.conf',
                               CONTAINERD_CONFIG=self.root / 'containerd.toml',
                               HOST_RESOLV=self.root / 'host-resolv.conf',
                               INSTANCE_ID=self.root / 'instance-id').items():
            patcher = mock.patch.object(bootstrap, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        bootstrap.INSTANCE_ID.write_text('i-test')
        bootstrap.CUSTOM.write_text('KUBELET_CUSTOMIZED_ARGS="--max-pods=100"\n')
        bootstrap.CONTAINERD_CONFIG.write_text('[plugins."io.containerd.cri.v1.images".registry]\nconfig_path="/etc/containerd/cert.d:/etc/containerd/certs.d"\n')
        self.resolver = self.root / 'resolv.conf'
        self.resolver.write_text('nameserver 192.0.2.53\nnameserver 192.0.2.54\noptions timeout:1 attempts:2\n')
        bootstrap.HOST_RESOLV.write_text(self.resolver.read_text())
        self.kubelet_config = self.root / 'kubelet.yaml'
        self.kubelet_config.write_text('clusterDNS:\n- 10.20.0.10\nresolvConf: ' + str(self.resolver) + '\n')
        self.arguments = ['kubelet', '--config=' + str(self.kubelet_config)]
        self.profile = dict(cluster_id='c-test', region='cn-hangzhou', registry=REGISTRY,
                            domain=DOMAIN, service_cidr='10.20.0.0/20')
        self.node = dict(metadata=dict(name='cn-hangzhou.192.0.2.2', uid='uid-test', resourceVersion='7',
                                      labels={bootstrap.PROFILE_LABEL: bootstrap.PROFILE, bootstrap.READY_LABEL: 'false', 'ack.aliyun.com': 'c-test'}),
                         spec=dict(providerID='cn-hangzhou.i-test'),
                         status=dict(conditions=[dict(type='Ready', status='True')]))
        self.commands = []
        self.patches = []
        self.nm_options = ''
        self.actual_hosts_roots = bootstrap.hosts_roots
        patchers = [mock.patch.object(bootstrap, 'run', side_effect=self.command),
                    mock.patch.object(bootstrap.time, 'sleep'),
                    mock.patch.object(bootstrap, 'kubelet_arguments', side_effect=lambda: list(self.arguments)),
                    mock.patch.object(bootstrap, 'hosts_roots', return_value=[self.root / 'hosts']),
                    mock.patch.object(bootstrap.Bootstrap, 'api', side_effect=self.api)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def api(self, method='GET', patch=None):
        if patch is not None:
            self.patches.append(patch)
            self.node['metadata']['labels'][bootstrap.READY_LABEL] = 'true'
        return copy.deepcopy(self.node)

    def command(self, arguments, timeout=30):
        self.commands.append(arguments)
        if arguments == ['systemctl', 'is-active', 'NetworkManager']:
            return 'active'
        if arguments[:4] == ['ip', '-j', 'route', 'show']:
            return '[{"dev":"eth0"}]'
        if arguments[:3] == ['nmcli', '-g', 'GENERAL.CON-UUID']:
            return '11111111-1111-1111-1111-111111111111'
        if arguments[:5] == ['nmcli', '--escape', 'no', '-g', 'ipv4.dns-options']:
            return self.nm_options
        if arguments[:3] == ['systemctl', 'restart', 'kubelet']:
            self.assertEqual(bootstrap.UPSTREAM.read_text(), self.resolver.read_text())
            self.arguments.append('--resolv-conf=' + str(bootstrap.UPSTREAM))
        if arguments[:3] == ['nmcli', 'connection', 'modify']:
            self.assertIn('--resolv-conf=' + str(bootstrap.UPSTREAM), self.arguments)
            self.nm_options = arguments[arguments.index('ipv4.dns-options') + 1].strip()
            servers = arguments[arguments.index('ipv4.dns') + 1].split(',')
            bootstrap.HOST_RESOLV.write_text(''.join('nameserver ' + value + '\n' for value in servers) +
                                           'options ' + self.nm_options.replace(',', ' ') + '\n')
        if arguments[0] == 'containerd':
            return bootstrap.CONTAINERD_CONFIG.read_text()
        return ''

    def test_service_absence_does_not_gate_and_reboots_are_idempotent(self):
        # Service DNS may be NXDOMAIN: configuration must still release the
        # worker. This is NOT a claim of live containerd fallback verification.
        with mock.patch.object(socket, 'getaddrinfo', side_effect=socket.gaierror('NXDOMAIN')) as lookup:
            bootstrap.Bootstrap(self.profile).start()
            lookup.assert_not_called()
        self.assertEqual(self.node['metadata']['labels'][bootstrap.READY_LABEL], 'true')
        mirror = self.root / 'hosts' / REGISTRY / 'hosts.toml'
        self.assertIn(('https://' + REGISTRY).encode(), mirror.read_bytes())
        self.assertNotIn('10.20.0.10', bootstrap.UPSTREAM.read_text())
        dns_change = next(command for command in self.commands if command[:3] == ['nmcli', 'connection', 'modify'])
        self.assertEqual(dns_change[dns_change.index('ipv4.dns') + 1], '10.20.0.10,192.0.2.53,192.0.2.54')
        self.assertEqual(self.patches[0][0], {'op': 'test', 'path': '/metadata/uid', 'value': 'uid-test'})
        original_mirror = mirror.read_bytes()
        bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(mirror.read_bytes(), original_mirror)
        self.assertEqual(len(self.patches), 1)
        self.assertEqual(sum(command[:3] == ['systemctl', 'restart', 'kubelet'] for command in self.commands), 1)

    def test_host_rotate_removed_but_original_pod_resolver_is_preserved(self):
        original = self.resolver.read_text() + 'options rotate\n'
        self.resolver.write_text(original)
        bootstrap.HOST_RESOLV.write_text(original)
        self.nm_options = 'timeout:1,attempts:2,rotate'
        bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(bootstrap.UPSTREAM.read_text(), original)
        self.assertNotIn('rotate', bootstrap.dns_options(bootstrap.HOST_RESOLV.read_text()))
        self.assertIn('timeout:1', bootstrap.dns_options(bootstrap.HOST_RESOLV.read_text()))
        bootstrap.time.sleep.assert_called_once_with(6)
        bootstrap.Bootstrap(self.profile).start()

    def test_effective_rotate_from_other_dns_configuration_does_not_release(self):
        original = self.command

        def retained_rotate(arguments, timeout=30):
            result = original(arguments, timeout)
            if arguments[:3] == ['nmcli', 'device', 'reapply']:
                with bootstrap.HOST_RESOLV.open('a') as stream:
                    stream.write('options rotate\n')
            return result

        with mock.patch.object(bootstrap, 'run', side_effect=retained_rotate):
            with self.assertRaisesRegex(RuntimeError, 'keep CoreDNS first'):
                bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(self.patches)

    def test_host_no_reload_cannot_be_marked_ready(self):
        with bootstrap.HOST_RESOLV.open('a') as stream:
            stream.write('options no-reload\n')
        with self.assertRaisesRegex(RuntimeError, 'no-reload is unsupported'):
            bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(self.patches)
        self.assertFalse(bootstrap.UPSTREAM.exists())

    def test_wrong_cluster_or_dragonfly_pool_cannot_change_host(self):
        for key, value in [('ack.aliyun.com', 'other-cluster'), ('node-role', 'dragonfly-seed')]:
            with self.subTest(key=key):
                original = copy.deepcopy(self.node)
                self.node['metadata']['labels'][key] = value
                with self.assertRaises(RuntimeError):
                    bootstrap.Bootstrap(self.profile).start()
                self.node = original
                self.assertFalse(self.commands)
                self.assertFalse(self.patches)

    def test_unsupported_transfer_path_cannot_change_dns_or_release_node(self):
        original_custom = bootstrap.CUSTOM.read_bytes()
        with mock.patch.object(bootstrap, 'hosts_roots', self.actual_hosts_roots):
            with self.assertRaisesRegex(RuntimeError, 'Transfer Service'):
                bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(self.patches)
        self.assertFalse(bootstrap.UPSTREAM.exists())
        self.assertEqual(bootstrap.CUSTOM.read_bytes(), original_custom)
        self.assertFalse(any(command[0] in ('systemctl', 'nmcli') for command in self.commands))

    def test_foreign_mirror_cannot_be_overwritten_or_released(self):
        mirror = self.root / 'hosts' / REGISTRY / 'hosts.toml'
        mirror.parent.mkdir(parents=True)
        mirror.write_text('existing operator mirror')
        with self.assertRaisesRegex(RuntimeError, 'foreign'):
            bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(mirror.read_text(), 'existing operator mirror')
        self.assertFalse(self.patches)
        self.assertFalse(any(command[:3] == ['nmcli', 'connection', 'modify'] for command in self.commands))

    def test_denied_own_node_access_and_failed_restart_keep_node_pending(self):
        with mock.patch.object(bootstrap.Bootstrap, 'api', side_effect=PermissionError('own-node denied')):
            with self.assertRaises(PermissionError):
                bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(self.commands)
        original = self.command

        def fail_restart(arguments, timeout=30):
            if arguments[:3] == ['systemctl', 'restart', 'kubelet']:
                raise RuntimeError('restart failed')
            return original(arguments, timeout)

        with mock.patch.object(bootstrap, 'run', side_effect=fail_restart):
            with self.assertRaisesRegex(RuntimeError, 'restart failed'):
                bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(self.patches)
        self.assertEqual(self.node['metadata']['labels'][bootstrap.READY_LABEL], 'false')

    def test_nm_success_without_effective_dns_does_not_release(self):
        original = self.command

        def ignored_dns(arguments, timeout=30):
            result = original(arguments, timeout)
            if arguments[:3] == ['nmcli', 'connection', 'modify']:
                bootstrap.HOST_RESOLV.write_text(self.resolver.read_text())
            return result

        with mock.patch.object(bootstrap, 'run', side_effect=ignored_dns):
            with self.assertRaisesRegex(RuntimeError, 'apply CoreDNS'):
                bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(self.patches)

    def test_external_dns_changes_after_bootstrap_are_not_overwritten(self):
        bootstrap.Bootstrap(self.profile).start()
        original = self.command

        def changed_dns(arguments, timeout=30):
            if arguments[:3] == ['nmcli', '-g', 'ipv4.dns']:
                return '192.0.2.99'
            return original(arguments, timeout)

        self.commands.clear()
        with mock.patch.object(bootstrap, 'run', side_effect=changed_dns):
            with self.assertRaisesRegex(RuntimeError, 'DNS changed outside'):
                bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(any(command[:3] == ['nmcli', 'connection', 'modify'] for command in self.commands))


class ProfileGenerationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        scripts = self.root / 'deploy/scripts'
        scripts.mkdir(parents=True)
        for name in ('configure.sh', 'common.sh'):
            shutil.copyfile(ROOT / 'deploy/scripts' / name, scripts / name)
        policy = self.root / 'deploy/terraform/aliyun/node-image-pull-policy.json'
        policy.parent.mkdir(parents=True)
        shutil.copyfile(ALIYUN / policy.name, policy)
        binary = self.root / 'bin'
        binary.mkdir()
        for name, body in [('terraform', 'exit 0'), ('git', 'echo abc1234')]:
            path = binary / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'])
        for key in ('TERRAFORM_STATE_FILE', 'TERRAFORM_DATA_DIR', 'TF_DATA_DIR'):
            self.environment.pop(key, None)
        self.profile = self.root / '.akernel/offline'

    def configure(self, *extra, ok=True):
        result = subprocess.run(['bash', str(self.root / 'deploy/scripts/configure.sh'), '--env', 'offline',
                                 '--non-interactive', '--image-tag', 'test-release', *extra],
                                env=self.environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def test_new_ack_profile_defaults_auto_and_optional_install_stays_false(self):
        self.configure()
        variables = (self.profile / 'terraform.tfvars').read_text()
        self.assertRegex(variables, r'node_image_pull_mode\s*=\s*"auto"')
        self.assertRegex(variables, r'install_dragonfly\s*=\s*false')
        self.assertIn('acr_registry_url = "registry.cn-hangzhou.aliyuncs.com/akernel"', variables)

    def test_custom_auto_repository_sets_matching_pull_secret_registry(self):
        repository = 'example-registry.ap-southeast-3.cr.aliyuncs.com/custom/all-in-one'
        self.configure('--image-repository', repository)
        variables = (self.profile / 'terraform.tfvars').read_text()
        self.assertIn('acr_registry_url = "' + repository.rsplit('/', 1)[0] + '"', variables)
        self.assertIn('node_image_repository   = "' + repository + '"', variables)

    def test_old_profile_regeneration_preserves_direct_and_needs_force(self):
        self.profile.mkdir(parents=True)
        variables = self.profile / 'terraform.tfvars'
        variables.write_text('original profile\n')
        self.configure(ok=False)
        self.assertEqual(variables.read_text(), 'original profile\n')
        self.configure('--force')
        self.assertRegex(variables.read_text(), r'node_image_pull_mode\s*=\s*"direct"')

    def test_other_registry_requires_direct(self):
        self.configure('--image-repository', 'example.com/akernel/all-in-one', ok=False)
        self.assertFalse(self.profile.exists())
        self.configure('--image-repository', 'example.com/akernel/all-in-one', '--node-image-pull-mode', 'direct')
        self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"direct"')

    def test_existing_auto_profile_keeps_auto(self):
        self.profile.mkdir(parents=True)
        (self.profile / 'terraform.tfvars').write_text('node_image_pull_mode = "auto" # existing choice\n')
        self.configure('--force')
        self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"auto"')

    def test_config_only_auto_profile_keeps_auto(self):
        self.profile.mkdir(parents=True)
        (self.profile / 'config.env').write_text('NODE_IMAGE_PULL_MODE=auto\n')
        self.configure('--force')
        self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"auto"')

    def test_state_only_recovery_never_implicitly_enables_auto(self):
        for relative in ('terraform.tfstate', 'terraform.tfstate.backup', '.terraform/terraform.tfstate'):
            with self.subTest(state=relative):
                if self.profile.exists():
                    shutil.rmtree(self.profile)
                path = self.profile / relative
                path.parent.mkdir(parents=True)
                path.write_text('{"resources":[{"type":"alicloud_cs_managed_kubernetes"}]}')
                self.configure()
                self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"direct"')

    def test_custom_state_and_backend_metadata_preserve_direct(self):
        for key, name in (('TERRAFORM_STATE_FILE', 'external.tfstate'),
                          ('TERRAFORM_STATE_FILE', 'external.tfstate.backup'),
                          ('TERRAFORM_DATA_DIR', 'backend/terraform.tfstate'),
                          ('TF_DATA_DIR', 'tf-data/terraform.tfstate')):
            with self.subTest(key=key, name=name):
                if self.profile.exists():
                    shutil.rmtree(self.profile)
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
                self.environment[key] = str(path.parent if key.endswith('DIR') else self.root / 'external.tfstate')
                self.configure()
                self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"direct"')
                del self.environment[key]
                path.unlink()

    def test_recovered_auto_state_preserves_mode_and_prefers_current_state(self):
        self.profile.mkdir(parents=True)
        state = self.profile / 'terraform.tfstate'
        backup = self.profile / 'terraform.tfstate.backup'
        auto = json.dumps({'resources': [dict(type='helm_release', name='akernel_core_dragonfly',
                                             instances=[dict(index_key=0, attributes={})])]})
        backup.write_text(auto)
        self.configure()
        self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"auto"')
        (self.profile / 'terraform.tfvars').unlink()
        (self.profile / 'config.env').unlink()
        state.write_text('{"resources": []}')
        self.configure()
        self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"direct"')

    def test_partial_auto_pool_state_is_preserved_but_explicit_direct_release_wins(self):
        self.profile.mkdir(parents=True)
        resources = [dict(type='alicloud_cs_kubernetes_node_pool', name='other', instances=[dict(attributes=dict(labels=None))]),
                     dict(type='alicloud_cs_kubernetes_node_pool', name='default_with_key', instances=[
            dict(index_key=0, attributes=dict(labels=[dict(key=bootstrap.PROFILE_LABEL, value=bootstrap.PROFILE)]))])]
        state = self.profile / 'terraform.tfstate'
        state.write_text(json.dumps(dict(resources=resources)))
        self.configure()
        self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"auto"')
        (self.profile / 'terraform.tfvars').unlink()
        (self.profile / 'config.env').unlink()
        resources.append(dict(type='helm_release', name='akernel_core', instances=[dict(attributes={})]))
        state.write_text(json.dumps(dict(resources=resources)))
        self.configure()
        self.assertRegex((self.profile / 'terraform.tfvars').read_text(), r'node_image_pull_mode\s*=\s*"direct"')

    def test_huawei_has_no_bootstrap_and_rejects_auto(self):
        self.configure('--vendor', 'huaweicloud', '--node-pool-key-name', 'test-key')
        self.assertNotIn('node_image_pull_mode', (self.profile / 'terraform.tfvars').read_text())
        self.configure('--vendor', 'huaweicloud', '--node-pool-key-name', 'test-key',
                       '--node-image-pull-mode', 'auto', '--force', ok=False)


if __name__ == '__main__':
    unittest.main()
