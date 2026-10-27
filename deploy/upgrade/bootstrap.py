#!/usr/bin/env python3
"""Start the upgrade executor in a node image without changing system Python."""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import zipfile


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    with tempfile.TemporaryDirectory(prefix="akernel-upgrade-") as directory:
        root = Path(directory)
        scripts, dependencies = root / "scripts", root / "dependencies"
        mounted = Path(__file__).resolve().parent
        payload = (mounted / "scripts.zip").read_bytes()
        scripts_hash = hashlib.sha256((mounted / "bootstrap.py").read_bytes() + payload).hexdigest()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            archive.extractall(scripts)
        sys.path.insert(0, str(scripts))
        from manage import parser

        cli = parser()
        args = cli.parse_args(argv)
        version = os.environ.get("AKERNEL_SDK_VERSION", "")
        if version and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version):
            cli.error("AKERNEL_SDK_VERSION must be an exact version, not a range or URL")
        if args.command in ("apply", "resume", "abort"):
            if not args.execute:
                cli.error("mutating commands require --execute")
            if not version:
                cli.error("mutating commands require an exact AKERNEL_SDK_VERSION from plan")

        # Resolve both clients together; --target leaves the node image untouched.
        sdk = f"akernel-sdk=={version}" if version else "akernel-sdk"
        install = [sys.executable, "-m", "pip", "install", "--no-cache-dir",
                   "--upgrade", "--target", str(dependencies),
                   "-r", str(scripts / "requirements.txt")]
        if not version:
            install.append("--pre")
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([*install, sdk], env=environment)
        if result.returncode:
            return result.returncode
        resolved = next((item.version for item in importlib.metadata.distributions(
            path=[str(dependencies)]) if item.metadata["Name"] == "akernel-sdk"), None)
        if not resolved:
            print("upgrade stopped: installed akernel-sdk metadata is missing", file=sys.stderr)
            return 2
        print(f"akernel-sdk={resolved}", flush=True)
        # -S excludes the image's system SDK, ADX packages and site .pth files.
        environment["PYTHONPATH"] = str(dependencies)
        environment["AKERNEL_UPGRADE_JOB_CONTEXT"] = json.dumps(
            {"scripts_sha256": scripts_hash, "sdk_version": resolved})
        os.execve(sys.executable, [sys.executable, "-S", str(scripts / "manage.py"), *argv],
                  environment)


if __name__ == "__main__":
    # Python is PID 1 in the Job. Exit on deletion even while pip is running;
    # subprocess.run kills/reaps its child when this handler interrupts it.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    raise SystemExit(main())
