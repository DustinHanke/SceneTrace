from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "blender" / "scenetrace"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "scenetrace-1.0.1.zip")
    args = parser.parse_args()
    staging = ROOT / "dist" / "staging" / "scenetrace"
    if staging.parent.exists():
        shutil.rmtree(staging.parent)
    shutil.copytree(ADDON, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", ZIP_DEFLATED) as archive:
        for path in staging.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(staging))
    print(args.output)


if __name__ == "__main__":
    main()
