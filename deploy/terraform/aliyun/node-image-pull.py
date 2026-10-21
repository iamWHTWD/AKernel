#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""ACK Aliyun Linux 3 worker bootstrap; Python 3.6+, no third-party packages.

Runs as a root-owned systemd service installed by node-pool user_data. The
kubelet client certificate authorizes access to this node only. Never install
an administrator kubeconfig here or wait for Dragonfly to become available.
"""

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


BASE = Path('/etc/akernel/image-pull')
CUSTOM = Path('/etc/kubernetes/kubelet-customized-args.conf')
UPSTREAM = BASE / 'kubelet-resolv.conf'
KUBELET_CONF = Path('/etc/kubernetes/kubelet.conf')
CONTAINERD_CONFIG = Path('/etc/containerd/config.toml')
HOST_RESOLV = Path('/etc/resolv.conf')
INSTANCE_ID = Path('/var/lib/cloud/data/instance-id')
PROFILE_LABEL = 'akernel.io/image-pull-profile'
READY_LABEL = 'akernel.io/image-pull-ready'
PROFILE = 'ack-dragonfly-v1'


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def atomic_write(path, content, mode=0o600):
    # Reject symlink ancestors as well as a symlink destination.
    require(not any(p.is_symlink() for p in [path] + list(path.parents)),
            'symlink in managed configuration path')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.bootstrap-', dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def owned_write(path, content, original=None):
    require(not path.is_symlink(), 'symlink configuration destination')
    if path.exists():
        require(path.read_bytes() in [content, original],
                'configuration changed outside bootstrap: ' + str(path))
    if not path.exists() or path.read_bytes() != content:
        atomic_write(path, content, 0o644)


def run(arguments, timeout=30):
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, env=dict(os.environ, PATH='/usr/sbin:/usr/bin:/sbin:/bin'))
    # Command output can contain configuration or credentials; do not log it.
    require(result.returncode == 0, 'command failed: ' + arguments[0])
    return result.stdout.decode().strip()


def scalar(text, key):
    matches = re.findall(r'(?m)^\s*' + re.escape(key) + r':\s*(\S+)\s*$', text)
    require(len(matches) == 1, 'missing or ambiguous configuration field: ' + key)
    return matches[0].strip('\"\'')


def argument(arguments, key, default=None):
    values = []
    for index, item in enumerate(arguments):
        if item.startswith(key + '='):
            values.append(item.split('=', 1)[1])
        elif item == key:
            require(index + 1 < len(arguments), 'incomplete kubelet argument')
            values.append(arguments[index + 1])
    require(len(values) <= 1, 'duplicate kubelet argument: ' + key)
    return values[0] if values else default


def kubelet_arguments():
    processes = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / 'comm').read_text().strip() == 'kubelet':
                processes.append([x.decode() for x in (entry / 'cmdline').read_bytes().split(b'\0') if x])
        except OSError:
            continue
    require(len(processes) == 1, 'waiting for one running kubelet')
    return processes[0]


def kubelet_dns(text):
    """Read only ACK's top-level scalar resolvConf and scalar-list clusterDNS."""
    if text.lstrip().startswith('{'):
        config = json.loads(text)
        return config['clusterDNS'], config.get('resolvConf', '/etc/resolv.conf')
    match = re.search(r'(?m)^clusterDNS:\s*(\[[^\n]*\])?\s*\n((?:[ \t]*-[^\n]*\n)*)', text)
    require(match is not None, 'unsupported kubelet clusterDNS configuration')
    if match.group(1):
        addresses = [x.strip().strip('\"\'') for x in match.group(1)[1:-1].split(',') if x.strip()]
    else:
        addresses = [x.strip().strip('\"\'') for x in re.findall(r'(?m)^\s*-\s*(\S+)\s*$', match.group(2))]
    resolver = re.findall(r'(?m)^resolvConf:\s*(\S+)\s*$', text)
    require(len(resolver) <= 1, 'ambiguous kubelet resolvConf')
    return addresses, resolver[0].strip('\"\'') if resolver else '/etc/resolv.conf'


def upstream_addresses(text, cluster_dns):
    addresses = re.findall(r'(?m)^\s*nameserver\s+(\S+)\s*(?:#.*)?$', text)
    result = []
    for value in addresses:
        address = ipaddress.ip_address(value)
        require(address.version == 4 and not address.is_loopback and not address.is_unspecified,
                'first release requires native IPv4 upstream DNS, without a local stub')
        require(value not in cluster_dns, 'upstream DNS points back to cluster DNS')
        if value not in result:
            result.append(value)
    require(bool(result) and len(result) <= 2, 'expected one or two original upstream DNS servers')
    return result


def dns_options(text):
    return [value for line in re.findall(r'(?m)^\s*options\s+([^#;\n]+)', text) for value in line.split()]


def nm_values(text):
    return [value for value in re.split(r'[,;\s]+', text) if value]


def customized_args(text):
    matches = list(re.finditer(r'(?m)^\s*(?:export\s+)?KUBELET_CUSTOMIZED_ARGS\s*=.*$', text))
    require(len(matches) <= 1, 'multiple kubelet customized argument assignments')
    arguments = []
    if matches:
        line = matches[0].group().strip()
        if line.startswith('export '):
            line = line[7:]
        tokens = shlex.split(line, comments=True)
        require(len(tokens) == 1 and tokens[0].startswith('KUBELET_CUSTOMIZED_ARGS='),
                'nonliteral kubelet customized arguments')
        arguments = shlex.split(tokens[0].split('=', 1)[1])
        require(not any('$' in item or '`' in item for item in arguments),
                'shell expansion in kubelet customized arguments')
    retained = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == '--resolv-conf':
            require(index + 1 < len(arguments), 'incomplete resolv-conf flag')
            index += 2
            continue
        if not item.startswith('--resolv-conf='):
            retained.append(item)
        index += 1
    retained.append('--resolv-conf=' + str(UPSTREAM))
    replacement = 'KUBELET_CUSTOMIZED_ARGS=' + shlex.quote(' '.join(shlex.quote(x) for x in retained))
    if matches:
        return (text[:matches[0].start()] + replacement + text[matches[0].end():]).encode()
    return (text.rstrip() + '\n' + replacement + '\n').lstrip('\n').encode()


def hosts_roots(text):
    # Only accept the active CRI registry section, not an unrelated config_path.
    section = ''
    paths = []
    local_pull = False
    for line in text.splitlines():
        header = re.fullmatch(r'\s*\[([^\[\]]+)\]\s*(?:#.*)?', line)
        if header:
            section = header.group(1).replace('\"', '').replace("'", '')
        if section == 'plugins.io.containerd.cri.v1.images':
            local_pull = local_pull or re.fullmatch(r'\s*use_local_image_pull\s*=\s*true\s*(?:#.*)?', line) is not None
        if section not in ('plugins.io.containerd.grpc.v1.cri.registry',
                           'plugins.io.containerd.cri.v1.images.registry'):
            continue
        match = re.fullmatch(r'\s*config_path\s*=\s*[\"\']([^\"\']+)[\"\']\s*(?:#.*)?', line)
        if match:
            paths.append((section, match.group(1)))
    require(len(paths) == 1, 'missing or ambiguous containerd CRI hosts config_path')
    section, path = paths[0]
    roots = path.split(':')
    require(bool(roots) and all(root in ('/etc/containerd/cert.d', '/etc/containerd/certs.d') for root in roots),
            'unsupported containerd CRI hosts config_path; main configuration is never overwritten')
    # Transfer Service (2.1+) treats config_path as one literal directory.
    # Config dump may not show runtime fallback to local pull; fail closed for
    # multi-root images-plugin configurations unless local pull is explicit.
    require(len(roots) == 1 or section == 'plugins.io.containerd.grpc.v1.cri.registry' or local_pull,
            'multi-directory config_path requires explicit local image pull; Transfer Service may bypass the mirror')
    return [Path(root) for root in roots]


def mirror_config(registry, domain):
    # ACR policy is checked by Terraform/profile generation. Here only validate
    # safe host/path/TOML syntax for the already trusted registry hostname.
    require(len(registry) <= 253 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*', registry) is not None,
            'invalid registry hostname')
    require(re.fullmatch(r'dragonfly-seed-client\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.svc\.cluster\.local', domain)
            is not None, 'invalid Dragonfly Service domain')
    endpoint = 'http://' + domain + ':4001'
    return ('# Managed by AKernel ACK image-pull bootstrap\n'
            'server = "https://' + registry + '"\n\n'
            '[host."' + endpoint + '"]\n'
            '  capabilities = ["pull"]\n'
            '  [host."' + endpoint + '".header]\n'
            '    X-Dragonfly-Registry = ["https://' + registry + '"]\n').encode()


class Bootstrap:
    def __init__(self, profile):
        self.profile = profile
        self.mirror = mirror_config(profile['registry'], profile['domain'])
        self.cluster_dns = None

    def api(self, method='GET', patch=None):
        config = KUBELET_CONF.read_text()
        server = scalar(config, 'server')
        parsed = urllib.parse.urlsplit(server)
        require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.path in ('', '/'), 'invalid kubelet API endpoint')
        if re.search(r'(?m)^\s*certificate-authority-data:', config):
            context = ssl.create_default_context(cadata=base64.b64decode(scalar(config, 'certificate-authority-data')).decode())
        else:
            context = ssl.create_default_context(cafile=scalar(config, 'certificate-authority'))
        cert = scalar(config, 'client-certificate')
        require(cert == scalar(config, 'client-key') == '/var/lib/kubelet/pki/kubelet-client-current.pem',
                'expected ACK rotating kubelet client certificate')
        subject = ssl._ssl._test_decode_cert(str(Path(cert).resolve()))['subject']
        names = [value for group in subject for key, value in group if key == 'commonName']
        require(len(names) == 1 and names[0].startswith('system:node:'), 'invalid kubelet node identity')
        name = names[0][len('system:node:'):]
        context.load_cert_chain(cert)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(server.rstrip('/') + '/api/v1/nodes/' + urllib.parse.quote(name, safe=''),
                                         data=canonical(patch) if patch is not None else None,
                                         headers={'Content-Type': 'application/json-patch+json'}, method=method)
        with opener.open(request, timeout=12) as response:
            node = json.load(response)
        require(node['metadata']['name'] == name, 'own-node response identity mismatch')
        return node

    def guard(self):
        node = self.api()
        metadata = node['metadata']
        labels = metadata.get('labels', {})
        instance = INSTANCE_ID.read_text().strip()
        require(labels.get('ack.aliyun.com') == self.profile['cluster_id'], 'ACK cluster mismatch')
        require(labels.get(PROFILE_LABEL) == PROFILE and labels.get(READY_LABEL) in ('false', 'true'),
                'node does not belong to this bootstrap profile')
        require(labels.get('node-role') not in ('dragonfly-seed', 'dragonfly-server'), 'Dragonfly pool excluded')
        require(node['spec'].get('providerID') == self.profile['region'] + '.' + instance
                and not metadata.get('deletionTimestamp'), 'ECS identity mismatch or deleting node')
        require(any(item['type'] == 'Ready' and item['status'] == 'True'
                    for item in node.get('status', {}).get('conditions', [])), 'waiting for own Node Ready')
        binding = dict(node=metadata['name'], uid=metadata['uid'], instance=instance,
                       profile_sha256=hashlib.sha256(canonical(self.profile)).hexdigest())
        path = BASE / 'binding.json'
        if path.exists():
            require(json.loads(path.read_text()) == binding, 'local node binding changed')
        else:
            atomic_write(path, canonical(binding))
        return node

    def prepare(self):
        arguments = kubelet_arguments()
        dns, resolver = kubelet_dns(Path(argument(arguments, '--config', '/etc/kubernetes/config')).read_text())
        require(len(dns) == 1 and ipaddress.ip_address(dns[0]) in ipaddress.ip_network(self.profile['service_cidr']),
                'first release requires one CoreDNS Service IP in the cluster Service CIDR')
        self.cluster_dns = dns[0]
        # Resolve imports/version migration instead of trusting a possibly
        # overridden config_path in the top-level TOML file.
        roots = hosts_roots(run(['containerd', '--config', str(CONTAINERD_CONFIG), 'config', 'dump']))
        self.hosts = roots[0] / self.profile['registry'] / 'hosts.toml'
        for root in roots[1:]:
            require(not (root / self.profile['registry']).exists(), 'competing containerd registry directory')
        require(run(['systemctl', 'is-active', 'NetworkManager']) == 'active', 'NetworkManager must be active')
        routes = json.loads(run(['ip', '-j', 'route', 'show', 'default']))
        require(len(routes) == 1, 'expected one default network route')
        device = routes[0]['dev']
        connection = run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', device])
        require(re.fullmatch(r'[a-f0-9-]{36}', connection) is not None, 'invalid NetworkManager connection')
        path = BASE / 'baseline.json'
        if path.exists():
            self.baseline = json.loads(path.read_text())
            require(self.baseline['device'] == device and self.baseline['connection'] == connection
                    and self.baseline['hosts'] == str(self.hosts), 'node network or hosts path changed')
        else:
            require(not self.hosts.exists() and not self.hosts.is_symlink() and not UPSTREAM.exists(),
                    'foreign registry mirror or upstream configuration; manual review required')
            original = Path(argument(arguments, '--resolv-conf', resolver)).read_text()
            self.baseline = dict(device=device, connection=connection, hosts=str(self.hosts),
                                 upstream=original, upstream_ips=upstream_addresses(original, dns),
                                 host_options=dns_options(HOST_RESOLV.read_text()),
                                 custom=CUSTOM.read_text() if CUSTOM.exists() else None,
                                 nm_dns=run(['nmcli', '-g', 'ipv4.dns', 'connection', 'show', connection]),
                                 nm_dns_options=run(['nmcli', '--escape', 'no', '-g', 'ipv4.dns-options', 'connection', 'show', connection]),
                                 nm_ignore_auto_dns=run(['nmcli', '-g', 'ipv4.ignore-auto-dns', 'connection', 'show', connection]))
            atomic_write(path, canonical(self.baseline))
        require('no-reload' not in self.baseline['host_options'],
                'host DNS no-reload is unsupported; containerd must reload the new resolver')

    def configure(self):
        baseline = self.baseline
        original = baseline['custom']
        wanted_dns = [self.cluster_dns] + baseline['upstream_ips']
        wanted_options = [value for value in baseline['host_options'] if value != 'rotate']
        current_dns = run(['nmcli', '-g', 'ipv4.dns', 'connection', 'show', baseline['connection']])
        current_ignore = run(['nmcli', '-g', 'ipv4.ignore-auto-dns', 'connection', 'show', baseline['connection']])
        current_options = run(['nmcli', '--escape', 'no', '-g', 'ipv4.dns-options', 'connection', 'show', baseline['connection']])
        require(nm_values(current_dns) in [nm_values(baseline['nm_dns']), wanted_dns]
                and current_ignore in [baseline['nm_ignore_auto_dns'], 'yes']
                and nm_values(current_options) in [nm_values(baseline['nm_dns_options']), wanted_options],
                'NetworkManager DNS changed outside bootstrap')
        owned_write(UPSTREAM, baseline['upstream'].encode())
        owned_write(CUSTOM, customized_args(original or ''), original.encode() if original is not None else None)
        if argument(kubelet_arguments(), '--resolv-conf') != str(UPSTREAM):
            run(['systemctl', 'restart', 'kubelet'], timeout=60)
        # A subsequent systemd attempt handles a delayed kubelet restart.
        require(argument(kubelet_arguments(), '--resolv-conf') == str(UPSTREAM),
                'waiting for kubelet to use the independent upstream resolver')
        run(['nmcli', 'connection', 'modify', baseline['connection'], 'ipv4.ignore-auto-dns', 'yes',
             'ipv4.dns', ','.join(wanted_dns), 'ipv4.dns-options', ','.join(wanted_options) or ' '])
        run(['nmcli', 'device', 'reapply', baseline['device']])
        effective_resolver = HOST_RESOLV.read_text()
        effective_dns = re.findall(r'(?m)^\s*nameserver\s+(\S+)\s*(?:#.*)?$', effective_resolver)
        require(effective_dns and effective_dns[0] == self.cluster_dns
                and all(address in effective_dns for address in baseline['upstream_ips']),
                'waiting for NetworkManager to apply CoreDNS and original upstream DNS on the host')
        require('rotate' not in dns_options(effective_resolver) and 'no-reload' not in dns_options(effective_resolver),
                'host resolver must keep CoreDNS first and allow runtime DNS reload')
        # No DNS lookup or Service readiness check: NXDOMAIN is a normal ACR fallback case.
        owned_write(self.hosts, self.mirror)
        # Go's resolver checks resolv.conf at most every five seconds. Let that
        # window expire before releasing the first AKernel pull; no Service is required.
        time.sleep(6)

    def release(self):
        node = self.guard()
        metadata = node['metadata']
        require(self.hosts.read_bytes() == self.mirror, 'mirror configuration changed before release')
        if metadata['labels'][READY_LABEL] != 'true':
            self.api('PATCH', [
                {'op': 'test', 'path': '/metadata/uid', 'value': metadata['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': metadata['resourceVersion']},
                {'op': 'test', 'path': '/metadata/labels/akernel.io~1image-pull-profile', 'value': PROFILE},
                {'op': 'replace', 'path': '/metadata/labels/akernel.io~1image-pull-ready', 'value': 'true'},
            ])
        atomic_write(BASE / 'status.json', canonical(dict(state='configured', node=metadata['name'],
                                                         mirror=self.profile['domain'], acr_fallback=True)))
        print('AKernel image-pull bootstrap configured; Dragonfly availability is not a readiness gate', flush=True)

    def start(self):
        self.guard()
        self.prepare()
        self.configure()
        self.release()


def main():
    require(os.geteuid() == 0 and not BASE.is_symlink() and BASE.stat().st_uid == 0
            and BASE.stat().st_mode & 0o077 == 0, 'root-owned private bootstrap directory required')
    profile = json.loads((BASE / 'profile.json').read_text())
    require(profile['helper_sha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'bootstrap checksum mismatch')
    Bootstrap(profile).start()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # No traceback, kubeconfig, command output, or HTTP response bodies in the journal.
        print('AKernel image-pull bootstrap pending: ' + type(error).__name__ + ': ' + str(error)[:200], flush=True)
        raise SystemExit(1)
