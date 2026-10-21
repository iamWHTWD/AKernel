# Aliyun One-Click Deployment (ACK + Helm)

This Terraform module deploys AKernel on Alibaba Cloud ACK. It can either:
- Create a new ACK cluster and node pool, then install Helm charts, or
- Reuse an existing Kubernetes cluster via kubeconfig.

It installs:
- `akernel-core` to `core_namespace`
- Optional `akernel-monitor` to `monitor_namespace` (controlled by `install_monitor`)
- Optional Dragonfly to `dragonfly_namespace` (controlled by `install_dragonfly`)
- Optional prereq `openkruise` to `prereq_namespace` (controlled by `install_prereqs`)

## Pod PID budget

`node_pool_pids_limit` defaults to `1620780` for the default pool and all
user-supplied `extra_node_pools`; generated Dragonfly pools are excluded.
Override it in `terraform.tfvars` with a positive integer or `-1` (node
allocatable PID capacity). All sandboxes and runtime services share this
budget; the per-sandbox limit stays 4096. Size it for node capacity and monitor
memory pressure. Existing clusters require separate kubelet configuration.
Review rollout impact before applying, then verify actual Pod and ancestor
`pids.max` values: a kubelet config update alone may leave existing Pods stale.

The default AKernel node pool also sets host `kernel.pid_max=4194304`, raises
`kernel.threads-max` to at least `4194304`, and removes implicit systemd limits
on container scopes. Settings persist through ACK's customized sysctl file
and systemd drop-ins; Pod and sandbox limits remain in force. Extra and
Dragonfly pools are unchanged. Existing hosts need a separately reviewed
migration, including kubelet restart if its node-wide PID capacity is stale;
changing user data only updates future nodes. Verify every Pod ancestor limit.

## Prerequisites
- Terraform >= 1.5
- Alibaba Cloud account permissions for VPC/VSwitch/ACK/RAM resources
- Aliyun credentials available to Terraform provider (for example via environment variables):

```bash
export ALICLOUD_ACCESS_KEY="<your-access-key-id>"
export ALICLOUD_SECRET_KEY="<your-access-key-secret>"
export ALICLOUD_REGION="cn-hangzhou"
```

## Quick Start (Create ACK + Install Helm)

The repository-level Makefile wraps this Terraform module and is the preferred
path for normal deployments:

```bash
make config VENDOR=aliyun
make plan
make deploy
make print-env
```

The helper keeps generated local files under `.akernel/default/`, including
`terraform.tfvars`, `terraform.tfstate`, `terraform.tfplan`, the generated
kubeconfig, and mode-0600 SDK credential files after `make print-env`. Pass
`ENV=<name>` for multiple independent deployment profiles.

For non-interactive agent usage:

```bash
make config \
  VENDOR=aliyun \
  NON_INTERACTIVE=1 \
  FORCE=1 \
  REGION=cn-hangzhou \
  ZONE_IDS=cn-hangzhou-j,cn-hangzhou-j,cn-hangzhou-j \
  NODE_POOL_KEY_NAME=<your-ecs-key-name> \
  IMAGE_REPOSITORY=registry.cn-hangzhou.aliyuncs.com/akernel/all-in-one \
  IMAGE_TAG=<existing-image-tag>
```

If you want to run Terraform directly:

```bash
cd deploy/terraform/aliyun
terraform init
terraform apply \
  -var 'region=cn-hangzhou' \
  -var 'zone_ids=["cn-hangzhou-i","cn-hangzhou-j","cn-hangzhou-k"]' \
  -var 'kubeconfig_output_path=/abs/path/to/generated/kubeconfig' \
  -var 'node_pool_key_name=<your-ssh-key-name>'
```

If you prefer login password instead of SSH key:

```bash
terraform apply \
  -var 'region=cn-hangzhou' \
  -var 'zone_ids=["cn-hangzhou-i","cn-hangzhou-j","cn-hangzhou-k"]' \
  -var 'node_pool_login_password=<your-strong-password>' \
  -var 'node_pool_key_name='
```

## Use Existing Cluster
When `create_cluster=false`, this module skips ACK/VPC creation and installs Helm releases to an existing cluster.

```bash
cd deploy/terraform/aliyun
terraform init
terraform apply \
  -var 'create_cluster=false' \
  -var 'region=cn-hangzhou' \
  -var 'zone_ids=["cn-hangzhou-i","cn-hangzhou-j","cn-hangzhou-k"]' \
  -var 'kubeconfig_path=/abs/path/to/kubeconfig'
```

## Common Options
Enable ACK API server public endpoint:

```bash
terraform apply -var 'api_server_public_access=true'
```

Install OpenKruise before AKernel:

```bash
terraform apply \
  -var 'install_prereqs=true' \
  -var 'prereq_namespace=kruise-system' \
  -var 'prereq_kruise_chart_version=1.8.3'
```

Install monitor chart:

```bash
terraform apply \
  -var 'install_monitor=true' \
  -var 'grafana_public_access=true' \
  -var 'monitor_storage_class=alicloud-disk-essd'
```

## AKernel node storage

New Terraform-managed ACK node pools attach two data disks by default. The
first remains available to ACK for the container runtime. The second is a
dedicated 300 GiB ESSD that ACK formats as XFS and mounts at
`/home/akernel` before the node joins the cluster. The same storage layout is
applied to user-defined `extra_node_pools`; dedicated Dragonfly pools retain
their own storage configuration.

AKernel stores both `/home/akernel/filestore` and
`/home/akernel/checkpoints` on that native XFS filesystem. The generated
sandboxd configuration intentionally leaves `filestore_dir_size` unset, so
sandboxd uses the directory directly instead of creating a loop-backed ext4
or XFS filesystem. Keeping the writable layer and checkpoint artifacts on
the same reflink-capable filesystem enables the fast Firecracker checkpoint
and restore path.

Change the dedicated disk capacity or category with
`node_pool_extra_data_disk_size` and
`node_pool_extra_data_disk_category`. Set
`node_pool_extra_data_disk_enabled=false` to opt out, for example when an
existing cluster already provisions `/home/akernel` itself. The option has no
effect when `create_cluster=false`; existing-cluster users must mount a
suitable host filesystem before deploying the chart. An explicit ext4
override remains supported for compatibility, but it cannot provide the XFS
reflink checkpoint path.

Changing these settings does not migrate live sandbox data on existing
nodes. Before replacing an existing node pool, drain its sandboxes and remove
or relocate lifecycle-bound checkpoints. Review the Terraform plan as the
default dedicated disk adds one cloud disk per AKernel node.

After provisioning a node, verify the effective layout with:

```bash
findmnt -no SOURCE,FSTYPE,TARGET /home/akernel
xfs_info /home/akernel | grep 'reflink=1'
```

Install Dragonfly P2P distribution:

```bash
terraform apply \
  -var 'install_dragonfly=true'
```

The module pins the public `dragonfly/dragonfly` chart at version `1.6.21`,
creates dedicated seed and server node pools by default, and injects
`http://dragonfly-seed-client.<namespace>.svc.cluster.local:4001` into the
AKernel node configuration. Set `oss_proxy_url` to override that address.
Review `dragonfly_seed_node_pool`, `dragonfly_server_node_pool`, storage, and
image registry settings before applying the plan.

## Worker image pulls through Dragonfly

New **Aliyun ACK** profiles generated with `make config` explicitly select
`NODE_IMAGE_PULL_MODE=auto`. `INSTALL_DRAGONFLY` remains independent and defaults
to `false`: without its Service, containerd falls back to the original ACR.
Select `INSTALL_DRAGONFLY=true` to provision the dedicated Dragonfly pools too.

| New-cluster configuration | AKernel node image pulls |
| --- | --- |
| Generated profile, `auto`, Dragonfly installed | Prefer Dragonfly for image content; retain ACR fallback |
| Generated profile, `auto`, no Dragonfly Service | Fall back to ACR; no Service readiness gate |
| `direct`, including raw Terraform defaults and legacy profiles | Pull directly from the configured registry |

This default covers new Aliyun ACK clusters created through `make config`,
with the supported host configuration below. It does not automatically opt
other cloud providers or existing clusters in, or install Dragonfly itself.

```bash
make config VENDOR=aliyun ENV=ack-new \
  NODE_IMAGE_PULL_MODE=auto INSTALL_DRAGONFLY=true \
  IMAGE_REPOSITORY=registry.cn-hangzhou.aliyuncs.com/akernel/all-in-one \
  IMAGE_TAG=<published-tag>
make plan ENV=ack-new
# Review the plan and obtain approval before make deploy ENV=ack-new.
```

First-release scope: newly provisioned clusters using official `AliyunLinux3`
images, native IPv4 upstream DNS, NetworkManager, Python 3.6+ and containerd
with an enabled `/etc/containerd/cert.d` or `certs.d` CRI hosts directory.
Containerd 1.x local pull can use both directories. For the 2.x images plugin,
multiple roots require explicit `use_local_image_pull=true`; a single root
works with local pull and Transfer Service. Other multi-root configurations
remain pending with an error, because Transfer Service can ignore their mirror.
The helper never rewrites containerd's main configuration to change pull mode.
Custom images, other operating systems, non-ACR registries, local DNS stubs and
NodeLocal DNS require a separate adaptation; use `direct` for those profiles.
The registry is derived from the effective `node_image_repository`, including
enterprise ACR overrides. The bootstrap does not embed registry credentials.
For private images, configure `acr_username`/`acr_password` through protected
inputs or an existing ACK credential provider. Generated auto profiles align
`acr_registry_url` with the selected image. When overriding `node_image_repository`
manually, keep that host equal to `acr_registry_url` when using the generated
`imagePullSecret`; even ACR public and VPC hostnames are distinct credential keys.

The default compute pool and user-defined `extra_node_pools` receive the same
image-pull bootstrap. Generated Dragonfly server and seed pools are excluded.
In auto mode only successfully initialized nodes can run the AKernel node
DaemonSet and ADX Redis, Coordinator and Ingress/API, which share the
all-in-one image.
This prevents control Pods from pre-pulling the image before the mirror exists.
Additional compute pools must be created through this module.
User-defined extra pools are compute pools, not alternate Dragonfly pools.

The startup order is:

1. ACK registers the node with `akernel.io/image-pull-ready=false`. CNI,
   CoreDNS and other system Pods can start normally.
2. Post-join User Data retains the existing module/PID setup and installs
   `akernel-image-pull.service`. It starts asynchronously and retries failures.
3. The root-owned helper verifies its own node/cluster/ECS identity using the
   kubelet client certificate. It saves the original DNS and customized kubelet
   arguments under `/etc/akernel/image-pull/baseline.json`.
4. Kubelet receives an independent copy of its original upstream resolver,
   through ACK's persistent `kubelet-customized-args.conf` mechanism. After the
   kubelet restart is confirmed, NetworkManager persists the existing CoreDNS
   Service IP ahead of the original upstream DNS servers on the host. Host
   `rotate` is removed while other DNS options and the Pod resolver are retained.
   This avoids forwarding CoreDNS back into itself; no CoreDNS ConfigMap changes.
5. The helper writes `hosts.toml`, then marks only its own node ready. Service
   existence and Dragonfly reachability do not gate this step. When auto mode
   installs Dragonfly, Terraform prepares it before AKernel core. Legacy direct
   profiles retain their independent Helm ordering.

The helper allows six seconds for containerd's Go resolver to reload the host
DNS before releasing the node. Hosts with `no-reload` remain pending and need
a separate adaptation. See [NetworkManager DNS options](https://www.networkmanager.dev/docs/api/latest/settings-ipv4.html)
and [Go's resolver cache](https://go.dev/src/net/dnsclient_unix.go).

For example, the generated registry file is:

```toml
server = "https://registry.cn-hangzhou.aliyuncs.com"

[host."http://dragonfly-seed-client.dragonfly-system.svc.cluster.local:4001"]
  capabilities = ["pull"]
  [host."http://dragonfly-seed-client.dragonfly-system.svc.cluster.local:4001".header]
    X-Dragonfly-Registry = ["https://registry.cn-hangzhou.aliyuncs.com"]
```

Tag resolution stays on ACR; blob pulls prefer Dragonfly. The `server` entry
preserves direct ACR fallback with the existing kubelet/ImagePullSecret
authentication and normal TLS validation. DNS/connection failures may add
runtime retry delays; fallback is not instantaneous, and still requires a
reachable ACR and valid credentials. A failed Dragonfly Helm installation is
reported as a deployment failure; runtime fallback does not hide that failure.
See [containerd's registry-host configuration](https://github.com/containerd/containerd/blob/v2.1.9/docs/hosts.md).

**Scope of impact:** host DNS changes apply to the new worker; the mirror
affects every Pod on that worker pulling from the same registry. It does not
select a repository or Pod. Sandbox image distribution continues to use the
existing `registry.proxy` configuration, independently of this host mirror.

The service validates/reapplies its owned files after reboot. It refuses
foreign mirrors and unexpected configuration drift, leaving new nodes pending
with a journal error instead of overwriting operator configuration. Diagnose
with `systemctl status akernel-image-pull` and
`journalctl -u akernel-image-pull`, then inspect
`/etc/akernel/image-pull/status.json`. Never copy kubelet credentials or the
baseline into reports. Own-node label PATCH permission is required; the helper
never grants itself cluster-wide RBAC.

Existing profiles that omit `node_image_pull_mode` keep `direct`. Regenerating
an old profile preserves its choice. State/backup can identify an existing auto
deployment, including auto pools created before Helm installation; other
state-only or backend-metadata recovery defaults to `direct`.
Auto is for new clusters: User Data configures only future nodes, while
readiness affinity applies immediately. Keep the mode
fixed after deployment; an existing-cluster migration requires a separate
rollout and state-address migration. The `moved` block preserves legacy
`helm_release.akernel_core` as `akernel_core[0]`; new auto deployments use
`akernel_core_dragonfly[0]`. Toggling `INSTALL_DRAGONFLY` does not change that
address. Changing the mode alone never restores DNS/mirrors already on hosts.

Run `make ack-image-pull-test` (Terraform and Helm required) and
`make deploy-script-check` before rollout.
Offline tests exercise profile compatibility, scoped writes, retry/idempotency,
resolver isolation and Service absence without contacting a cluster. They do
not establish real CRI fallback or throughput. On a disposable new ACK cluster,
verify seed logs for the initial pull, complete-image fallback with Service
NXDOMAIN and unreachable endpoints, subsequent scale-out, image upgrades/cache
eviction, and reboot persistence before production use.

## Public endpoint model

The default cloud deployment exposes the embedded ADX Ingress directly through
`akernel-adx-public`, a two-port LoadBalancer Service:

- HTTPS 443 forwards API, command/file and PTY traffic to control port 8443.
- HTTP 80 forwards sandbox port-forwarding traffic to data port 8080.

ADX terminates HTTPS with the certificate in `akernel-adx-tls`. Use the
LoadBalancer host or IP with the existing SDK variables:

```bash
export AKERNEL_SERVER_ADDRESS=<adx-load-balancer-address>
```

`adx_public_service_enabled` controls exposure; `adx_service_annotations`
retains cloud load-balancer and security-group settings. `adx_control_port`
and `adx_data_port` select distinct external ports. `make print-env` reads
them from the public Service and generates a gateway override for custom ports.

Enable OSS auth injection for AKernel node secret:

```bash
terraform apply \
  -var 'oss_endpoint=oss-cn-hangzhou.aliyuncs.com' \
  -var 'oss_access_key_id=<oss-ak>' \
  -var 'oss_access_key_secret=<oss-sk>' \
  -var 'oss_bucket=<bucket-name>'
```

## Key Variable Notes
- `zone_ids` is required and should match `vswitch_cidrs` length when `create_cluster=true`.
- `create_cluster=true` requires at least 3 `vswitch_cidrs/zone_ids` (zones can repeat).
- `node_pool_key_name` and `node_pool_login_password` are mutually exclusive. Set only one.
- If neither SSH key nor login password is set, Terraform passes `password = null` to node pool creation.
- `kubeconfig_output_path` controls where the generated kubeconfig is written when `create_cluster=true`.
- `oss_auths` and `registry_auths` accept strongly typed credential maps; keep
  their generated `terraform.tfvars` private.
- `dragonfly_chart_repository` and `dragonfly_chart_version` select the
  public pinned chart; override the repository when using a private mirror.

## Outputs and Verification
Get kubeconfig path:

```bash
terraform output -raw kubeconfig_path
```

Basic checks:

```bash
KUBECONFIG=$(terraform output -raw kubeconfig_path)
kubectl --kubeconfig "${KUBECONFIG}" get ns
kubectl --kubeconfig "${KUBECONFIG}" -n akernel get pods
kubectl --kubeconfig "${KUBECONFIG}" -n akernel-monitor get pods
```

## Uninstall
```bash
cd deploy/terraform/aliyun
terraform destroy
```
