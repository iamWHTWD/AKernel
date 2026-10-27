#!/usr/bin/env python3
"""Print an immutable scripts ConfigMap manifest; never contact the cluster."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent


def manifest(namespace):
    bootstrap = (ROOT / "bootstrap.py").read_text()
    files = [ROOT / "manage.py", ROOT / "requirements.txt",
             *sorted((ROOT / "akernel_upgrade").rglob("*.py"))]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            # Fixed timestamps make unchanged source produce the same name.
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    payload = buffer.getvalue()
    if len(bootstrap.encode()) + len(payload) > 1024 * 1024:
        raise ValueError("upgrade scripts exceed the ConfigMap 1 MiB limit")
    digest = hashlib.sha256(bootstrap.encode() + payload).hexdigest()[:16]
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"akernel-node-upgrade-scripts-{digest}",
                         "namespace": namespace},
            "immutable": True, "data": {"bootstrap.py": bootstrap},
            "binaryData": {"scripts.zip": base64.b64encode(payload).decode("ascii")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="akernel")
    args = parser.parse_args()
    print(json.dumps(manifest(args.namespace), indent=2))


if __name__ == "__main__":
    main()
