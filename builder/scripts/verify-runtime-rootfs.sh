#!/bin/sh
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -eu
if [ "$#" -ne 3 ]; then
    echo "usage: $0 ROOTFS_IMAGE EXPECTED_SHA256 TARGETARCH" >&2
    exit 2
fi
rootfs=$1
expected=$2
target_arch=$3
case "$expected" in
    ''|*[!0-9a-f]*) echo "invalid rootfs SHA-256" >&2; exit 1 ;;
esac
[ "${#expected}" -eq 64 ] || { echo "invalid rootfs SHA-256" >&2; exit 1; }
[ -f "$rootfs" ] && [ ! -L "$rootfs" ] || {
    echo "runtime rootfs must be a regular file" >&2; exit 1;
}
actual=$(sha256sum < "$rootfs")
[ "${actual%% *}" = "$expected" ] || {
    echo "runtime rootfs SHA-256 mismatch" >&2; exit 1;
}

# Check the embedded executable, not the source image's architecture label.
# Do not mount or execute the old image, and never modify its rootfs bytes.
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
trap 'exit 1' HUP INT TERM
fsck.erofs --extract="$scratch/rootfs" "$rootfs"
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
sh "$script_dir/verify-elf-arch.sh" "$scratch/rootfs/usr/local/bin/adx-execd" "$target_arch"
