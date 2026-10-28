# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Copy a published all-in-one image's rootfs without recreating its EROFS
# superblock. The helper requires a digest-pinned source and expected file hash.
ARG AKERNEL_RUNTIME_SOURCE_IMAGE
ARG AKERNEL_RUNTIME_BASE_IMAGE=ubuntu:24.04
FROM ${AKERNEL_RUNTIME_SOURCE_IMAGE} AS runtime-source

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS runtime-verified
ARG AKERNEL_RUNTIME_ROOTFS_SHA256
ARG TARGETARCH
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh
RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    apt-get install -y --no-install-recommends erofs-utils && \
    rm -rf /var/lib/apt/lists/*
COPY --from=runtime-source /opt/akernel/runtime/akernel-runtime-rootfs.img /akernel-runtime-rootfs.img
COPY builder/scripts/verify-runtime-rootfs.sh builder/scripts/verify-elf-arch.sh /usr/local/libexec/
RUN sh /usr/local/libexec/verify-runtime-rootfs.sh \
    /akernel-runtime-rootfs.img "${AKERNEL_RUNTIME_ROOTFS_SHA256}" "${TARGETARCH}"

FROM scratch AS runtime-execd
COPY --from=runtime-verified /akernel-runtime-rootfs.img /akernel-runtime-rootfs.img
LABEL org.akernel.runtime.profile="execd"
