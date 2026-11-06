#!/usr/bin/env python3
"""Build the HACS release zip for the ha_banking integration.

Usage:
    python3 scripts/build_release.py            # use version from manifest.json
    python3 scripts/build_release.py 0.2.0      # set manifest version, then build

Produces dist/ha_banking.zip with the integration files at the zip root,
as expected by HACS when hacs.json has "zip_release": true.
Attach that file to the GitHub release whose tag matches the version.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "ha_banking"
MANIFEST = INTEGRATION / "manifest.json"
HACS = ROOT / "hacs.json"
DIST = ROOT / "dist"
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
# Fixed timestamp so identical sources produce an identical zip.
ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())

    if len(sys.argv) > 1:
        version = sys.argv[1].removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+([.-]?\w+)?", version):
            print(f"Invalid version: {version}", file=sys.stderr)
            return 1
        manifest["version"] = version
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"manifest.json version set to {version}")
    version = manifest["version"]

    filename = json.loads(HACS.read_text()).get("filename", "ha_banking.zip")
    DIST.mkdir(exist_ok=True)
    out = DIST / filename

    files = sorted(
        p
        for p in INTEGRATION.rglob("*")
        if p.is_file()
        and p.suffix not in EXCLUDE_SUFFIXES
        and not EXCLUDE_DIRS.intersection(p.relative_to(INTEGRATION).parts)
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(INTEGRATION).as_posix(), ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())

    print(f"Built {out.relative_to(ROOT)} (version {version}, {len(files)} files)")
    for path in files:
        print(f"  {path.relative_to(INTEGRATION).as_posix()}")
    print(
        f"\nNext: commit, tag v{version}, push, and attach {out.name} to the "
        f"GitHub release, e.g.\n  gh release create v{version} {out.relative_to(ROOT)} --generate-notes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
