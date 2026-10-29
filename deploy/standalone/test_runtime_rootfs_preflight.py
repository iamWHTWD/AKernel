# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the rootfs probe's failures and cleanup without mounting anything."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("runtime-rootfs-preflight.sh")


class RuntimeRootfsPreflightTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = self.directory / "calls"
        self.sentinel = self.directory / "unrelated"
        self.sentinel.write_text("preserved")

    def run_probe(self, **overrides):
        environment = dict(os.environ)
        environment.pop("BASH_ENV", None)
        environment.pop("ENV", None)
        environment.update(
            MOCK_DIRECTORY=str(self.directory), MOCK_CALLS=str(self.calls),
            MOCK_SCRIPT=str(SCRIPT), MOCK_LOOP="0", MOCK_MOUNT="0",
            MOCK_UNMOUNT="0", MOCK_EXECD="0",
        )
        environment.update(overrides)
        stubs = r'''
record() { printf '%s\n' "$*" >> "$MOCK_CALLS"; }
test() {
    if [[ "$*" == '-c /dev/loop-control' ]]; then return "$MOCK_LOOP"; fi
    if [[ "$1" == -x && "$2" == */usr/local/bin/adx-execd ]]; then return "$MOCK_EXECD"; fi
    builtin test "$@"
}
mktemp() { command mktemp -d "$MOCK_DIRECTORY/probe.XXXXXX"; }
mount() { record mount "$@"; return "$MOCK_MOUNT"; }
umount() { record umount "$@"; return "$MOCK_UNMOUNT"; }
source "$MOCK_SCRIPT"
'''
        return subprocess.run(
            ["/bin/bash", "-c", stubs], env=environment, capture_output=True,
            text=True, timeout=10, check=False,
        )

    def assert_clean(self):
        self.assertEqual(list(self.directory.glob("probe.*")), [])
        self.assertEqual(self.sentinel.read_text(), "preserved")

    def test_readonly_bundled_image_mount_is_unmounted(self):
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertIn(
            "mount -t erofs -o loop,ro /opt/akernel/runtime/akernel-runtime-rootfs.img ",
            calls[0],
        )
        self.assertTrue(calls[1].startswith("umount "))
        self.assert_clean()

    def test_missing_loop_device_fails_without_mounting(self):
        result = self.run_probe(MOCK_LOOP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/dev/loop-control is unavailable", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assert_clean()

    def test_mount_failure_cleans_directory_without_unmounting(self):
        result = self.run_probe(MOCK_MOUNT="32")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("host EROFS and loop support are required", result.stderr)
        self.assertNotIn("umount", self.calls.read_text())
        self.assert_clean()

    def test_missing_execd_still_unmounts_rootfs(self):
        result = self.run_probe(MOCK_EXECD="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("executable adx-execd", result.stderr)
        self.assertIn("umount", self.calls.read_text())
        self.assert_clean()

    def test_unmount_failure_fails_without_removing_mountpoint(self):
        result = self.run_probe(MOCK_UNMOUNT="32")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("preflight cleanup failed", result.stderr)
        self.assertEqual(len(list(self.directory.glob("probe.*"))), 1)
        self.assertEqual(self.sentinel.read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
