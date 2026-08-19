from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "blender" / "scenetrace"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the SceneTrace Blender extension")
    parser.add_argument("--blender", default=os.environ.get("BLENDER_PATH", "blender"))
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "scenetrace-1.0.2.zip")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            args.blender,
            "--factory-startup",
            "--command",
            "extension",
            "build",
            "--source-dir",
            str(ADDON),
            "--output-filepath",
            str(args.output),
        ],
        check=True,
    )
    print(args.output)


if __name__ == "__main__":
    main()
