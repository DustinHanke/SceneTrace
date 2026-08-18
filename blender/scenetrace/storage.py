# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import bpy


def project_dir() -> Path | None:
    if not bpy.data.filepath:
        return None
    return Path(bpy.data.filepath).resolve().parent


def trace_dir() -> Path | None:
    root = project_dir()
    return root / ".scenetrace" if root else None


def baseline_path() -> Path | None:
    root = trace_dir()
    return root / "baseline.json" if root else None


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))
        raise OSError(f"could not atomically replace {path}; existing data was preserved")
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_baseline() -> dict | None:
    path = baseline_path()
    return load_json(path) if path and path.is_file() else None


def save_baseline(baseline: dict) -> Path:
    path = baseline_path()
    if path is None:
        raise RuntimeError("Save the .blend file before creating a SceneTrace baseline")
    baseline.setdefault("schema_version", 1)
    baseline.setdefault("scenetrace_version", "1.0.2")
    baseline.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    save_json(path, baseline)
    return path


def save_report(run: dict, comparison: dict | None = None, changes: list[dict] | None = None) -> Path:
    root = trace_dir()
    if root is None:
        raise RuntimeError("Save the .blend file before exporting a SceneTrace report")
    path = root / "latest.json"
    payload = {
        "schema": "scenetrace-interactive-report",
        "schema_version": 1,
        "scenetrace_version": "1.0.2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run": run,
    }
    if comparison is not None:
        payload["comparison"] = comparison
    if changes is not None:
        payload["correlated_changes"] = changes
    save_json(path, payload)
    return path
