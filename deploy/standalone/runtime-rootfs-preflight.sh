#!/usr/bin/env bash
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
# Run in a disposable privileged container without host mount propagation.
set -euo pipefail

if ! test -c /dev/loop-control; then
    echo 'The bundled runsc EROFS root requires host loop devices; /dev/loop-control is unavailable' >&2
    exit 1
fi

probe_dir="$(mktemp -d /tmp/akernel-runtime-rootfs.XXXXXX)"
rootfs_mounted=false
cleanup() {
    local status=$?
    trap - EXIT
    if [[ "${rootfs_mounted}" == true ]] && ! umount "${probe_dir}"; then
        echo "Runtime rootfs preflight cleanup failed: ${probe_dir}" >&2
        if [[ "${status}" == 0 ]]; then status=1; fi
    elif ! rmdir "${probe_dir}"; then
        echo "Runtime rootfs preflight cleanup failed: ${probe_dir}" >&2
        if [[ "${status}" == 0 ]]; then status=1; fi
    fi
    exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if ! mount -t erofs -o loop,ro /opt/akernel/runtime/akernel-runtime-rootfs.img "${probe_dir}"; then
    echo 'Cannot mount the bundled runtime rootfs; host EROFS and loop support are required for runsc initialization' >&2
    exit 1
fi
rootfs_mounted=true
if ! test -x "${probe_dir}/usr/local/bin/adx-execd"; then
    echo 'The bundled runtime rootfs does not contain executable adx-execd' >&2
    exit 1
fi
echo 'Bundled runtime EROFS and loop mount probe passed'
