"""Offline coverage for ConfigMap packaging and the node-image startup boundary."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import scripts_configmap


class ConfigMapTests(unittest.TestCase):
    def test_manifest_is_stable_and_contains_only_runtime_sources(self):
        value = scripts_configmap.manifest("poc")
        self.assertEqual(value, scripts_configmap.manifest("poc"))
        self.assertTrue(value["immutable"])
        self.assertEqual(value["metadata"]["namespace"], "poc")
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(
                value["binaryData"]["scripts.zip"]))) as archive:
            expected = {"manage.py", "requirements.txt", *(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / "akernel_upgrade").rglob("*.py"))}
            self.assertEqual(set(archive.namelist()), expected)
            self.assertEqual(archive.read("akernel_upgrade/__init__.py"),
                             (ROOT / "akernel_upgrade/__init__.py").read_bytes())
        self.assertEqual(set(value["data"]), {"bootstrap.py"})

    def test_source_change_produces_new_configmap_without_bundling_adjacent_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("manage.py", "requirements.txt", "bootstrap.py"):
                shutil.copyfile(ROOT / name, root / name)
            shutil.copytree(ROOT / "akernel_upgrade", root / "akernel_upgrade")
            (root / "secret.json").write_text("private-token")
            with patch.object(scripts_configmap, "ROOT", root):
                original = scripts_configmap.manifest("poc")
                (root / "bootstrap.py").write_text("# changed bootstrap\n")
                changed = scripts_configmap.manifest("poc")
                self.assertNotEqual(original["metadata"]["name"], changed["metadata"]["name"])
                (root / "akernel_upgrade/new_module.py").write_text("# new runtime module\n")
                added = scripts_configmap.manifest("poc")
                self.assertNotEqual(changed["metadata"]["name"], added["metadata"]["name"])
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(
                        added["binaryData"]["scripts.zip"]))) as archive:
                    self.assertIn("akernel_upgrade/new_module.py", archive.namelist())
                    self.assertNotIn("secret.json", archive.namelist())


# A disposable interpreter supplies a fake pip: tests perform no network access.
FAKE_PIP = '''
import json, os, pathlib, sys, time
pathlib.Path(os.environ["INSTALL_LOG"]).write_text(json.dumps(sys.argv[1:]))
target = pathlib.Path(sys.argv[sys.argv.index("--target") + 1])
target.mkdir(parents=True)
(target / "job_dependency.py").write_text('ORIGIN = "installed"\\n')
if os.environ.get("INSTALL_PID"):
    pathlib.Path(os.environ["INSTALL_PID"]).write_text(str(os.getpid()))
    time.sleep(60)
if os.environ.get("FAIL_INSTALL"):
    raise SystemExit(8)
metadata = target / "akernel_sdk-9.0rc1.dist-info"
metadata.mkdir()
(metadata / "METADATA").write_text("Name: akernel-sdk\\nVersion: 9.0rc1\\n")
'''

FAKE_MANAGE = '''
import argparse, json, os, sys
def parser():
    cli = argparse.ArgumentParser()
    cli.add_argument("command", nargs="?", default="plan",
                     choices=("plan", "status", "apply", "resume", "abort"))
    cli.add_argument("--config", required=True)
    cli.add_argument("--execute", action="store_true")
    return cli
if __name__ == "__main__":
    import job_dependency
    args = parser().parse_args()
    print(json.dumps({"command": args.command, "execute": args.execute,
                      "config": args.config, "origin": job_dependency.ORIGIN,
                      "no_site": sys.flags.no_site,
                      "context": json.loads(os.environ["AKERNEL_UPGRADE_JOB_CONTEXT"])}))
    raise SystemExit(7)
'''


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.TemporaryDirectory()
        runtime = Path(cls.runtime.name)
        venv.EnvBuilder(with_pip=False).create(runtime / "python")
        cls.python = runtime / "python/bin/python3"
        purelib = subprocess.check_output([str(cls.python), "-c",
            'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip()
        pip = Path(purelib) / "pip"
        pip.mkdir()
        (pip / "__init__.py").touch()
        (pip / "__main__.py").write_text(FAKE_PIP)
        # A system copy must never override the freshly installed dependencies.
        (Path(purelib) / "job_dependency.py").write_text('ORIGIN = "system"\n')

    @classmethod
    def tearDownClass(cls):
        cls.runtime.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        shutil.copyfile(ROOT / "bootstrap.py", self.root / "bootstrap.py")
        with zipfile.ZipFile(self.root / "scripts.zip", "w") as archive:
            archive.writestr("manage.py", FAKE_MANAGE)
            archive.writestr("requirements.txt", "kubernetes>=28,<35\n")
        self.environment = dict(os.environ, TMPDIR=str(self.root),
            INSTALL_LOG=str(self.root / "install.json"), AKERNEL_SDK_VERSION="",
            PYTHONDONTWRITEBYTECODE="1")
        self.environment.pop("FAIL_INSTALL", None)
        self.environment.pop("INSTALL_PID", None)

    def run_bootstrap(self, *args, version="", fail=False):
        environment = dict(self.environment, AKERNEL_SDK_VERSION=version)
        if fail:
            environment["FAIL_INSTALL"] = "1"
        return subprocess.run([str(self.python), "-S", str(self.root / "bootstrap.py"),
                               *args, "--config", "config with spaces.json"],
                              env=environment, capture_output=True, text=True)

    def test_latest_plan_installs_once_and_executes_with_isolated_dependencies(self):
        # Even an inherited PYTHONPATH must not leak into the executor.
        (self.root / "job_dependency.py").write_text('ORIGIN = "inherited"\n')
        self.environment["PYTHONPATH"] = str(self.root)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("akernel-sdk=9.0rc1", result.stdout)
        report = json.loads(result.stdout.splitlines()[-1])
        context = report.pop("context")
        self.assertEqual(report, {"command": "plan", "execute": False,
            "config": "config with spaces.json", "origin": "installed", "no_site": 1})
        self.assertEqual(context, {"sdk_version": "9.0rc1", "scripts_sha256":
            hashlib.sha256((self.root / "bootstrap.py").read_bytes()
                           + (self.root / "scripts.zip").read_bytes()).hexdigest()})
        install = json.loads((self.root / "install.json").read_text())
        self.assertIn("--pre", install)
        self.assertIn("--no-cache-dir", install)
        self.assertEqual(install[-1], "akernel-sdk")

    def test_all_mutating_commands_forward_authorization_and_pin(self):
        for command in ("apply", "resume", "abort"):
            with self.subTest(command=command):
                result = self.run_bootstrap(command, "--execute", version="9.0rc1")
                self.assertEqual(result.returncode, 7, result.stderr)
                report = json.loads(result.stdout.splitlines()[-1])
                self.assertEqual(report["command"], command)
                self.assertTrue(report["execute"])
                install = json.loads((self.root / "install.json").read_text())
                self.assertEqual(install[-1], "akernel-sdk==9.0rc1")
                self.assertNotIn("--pre", install)

    def test_write_without_version_or_execute_never_installs_or_starts(self):
        for command in ("apply", "resume", "abort"):
            for options, version in ((["--execute"], ""), ([], "9.0rc1")):
                with self.subTest(command=command, options=options):
                    result = self.run_bootstrap(command, *options, version=version)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse((self.root / "install.json").exists())

    def test_version_range_is_rejected_before_install(self):
        for version in (">=9", "9.*", "https://example/sdk.whl", "9.0 --pre"):
            with self.subTest(version=version):
                result = self.run_bootstrap("plan", version=version)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "install.json").exists())

    def test_failed_dependency_install_never_starts_executor(self):
        result = self.run_bootstrap("apply", "--execute", version="9.0rc1", fail=True)
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertNotIn('"command"', result.stdout)
        self.assertNotIn("akernel-sdk=", result.stdout)
        self.assertFalse(list(self.root.glob("akernel-upgrade-*")))
        # A fresh attempt must install into a fresh directory after partial pip failure.
        retried = self.run_bootstrap("apply", "--execute", version="9.0rc1")
        self.assertEqual(retried.returncode, 7, retried.stderr)

    def test_missing_or_corrupt_script_archive_never_installs_or_starts(self):
        archive = self.root / "scripts.zip"
        for contents in (None, b"not-a-zip"):
            if contents is None:
                archive.unlink()
            else:
                archive.write_bytes(contents)
            with self.subTest(contents=contents):
                result = self.run_bootstrap("apply", "--execute", version="9.0rc1")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "install.json").exists())
                self.assertFalse(list(self.root.glob("akernel-upgrade-*")))

    def test_executor_cli_handles_termination_before_any_external_access(self):
        ready = self.root / "cli-ready"
        # Pause in the real CLI parser, before config/client construction. This
        # runs its actual __main__ signal setup without calling Kubernetes/ADX.
        program = f'''
import argparse, pathlib, runpy, sys, time
sys.path.insert(0, {str(ROOT)!r})
def wait(*args, **kwargs):
    pathlib.Path({str(ready)!r}).write_text("ready")
    time.sleep(60)
argparse.ArgumentParser.parse_args = wait
runpy.run_path({str(ROOT / 'manage.py')!r}, run_name="__main__")
'''
        process = subprocess.Popen([str(self.python), "-S", "-c", program],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "executor CLI did not start")
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, stderr)

    def test_termination_during_install_reaps_pip_and_never_starts_executor(self):
        pid_file = self.root / "pip.pid"
        environment = dict(self.environment, INSTALL_PID=str(pid_file), AKERNEL_SDK_VERSION="9.0rc1")
        process = subprocess.Popen([str(self.python), "-S", str(self.root / "bootstrap.py"),
            "apply", "--execute", "--config", "unused.json"],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        pip_pid = ""
        while process.poll() is None and time.monotonic() < deadline:
            if pid_file.exists():
                pip_pid = pid_file.read_text().strip()
                if pip_pid:
                    break
            time.sleep(0.02)
        self.assertTrue(pip_pid, "fake pip did not start")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, stderr)
        self.assertNotIn('"command"', stdout)
        self.assertFalse(list(self.root.glob("akernel-upgrade-*")))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pip_pid), 0)

    def test_real_bundle_imports_and_help_without_external_dependencies(self):
        value = scripts_configmap.manifest("poc")
        (self.root / "scripts.zip").write_bytes(base64.b64decode(value["binaryData"]["scripts.zip"]))
        result = self.run_bootstrap("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--execute", result.stdout)
        self.assertFalse((self.root / "install.json").exists())


if __name__ == "__main__":
    unittest.main()
