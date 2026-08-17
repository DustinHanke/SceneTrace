from __future__ import annotations

"""SceneTrace background-mode entry point.

This module is executed by Blender via ``--python headless.py -- ...``.  It
reuses the same benchmark/analysis implementation as the interactive add-on,
but writes separate headless artifacts so background and interactive timings
are never compared accidentally.
"""

import argparse
import json
import os
import platform
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Blender executes --python files as __main__, so make the package importable
# when this file is invoked directly from a source checkout.
if __package__ in (None, ""):
    PACKAGE_PARENT = Path(__file__).resolve().parents[1]
    if str(PACKAGE_PARENT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_PARENT))

import bpy

from scenetrace.analysis import build_baseline_profile, build_diagnosis, compare_runs
from scenetrace.benchmark import run_benchmark
from scenetrace.snapshot import diff_snapshots


TOOL_VERSION = "1.0.1"
ARTIFACT_SCHEMA_VERSION = 1


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
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


def _dependency_records() -> list[dict]:
    records = []
    collections = (
        ("library", getattr(bpy.data, "libraries", ())),
        ("image", getattr(bpy.data, "images", ())),
        ("sound", getattr(bpy.data, "sounds", ())),
        ("movie_clip", getattr(bpy.data, "movieclips", ())),
        ("cache", getattr(bpy.data, "cache_files", ())),
        ("volume", getattr(bpy.data, "volumes", ())),
    )
    for kind, datablocks in collections:
        for datablock in datablocks:
            if getattr(datablock, "packed_file", None) is not None:
                continue
            raw_path = getattr(datablock, "filepath", "")
            if not raw_path:
                continue
            resolved = Path(bpy.path.abspath(raw_path)).resolve(strict=False)
            try:
                state = resolved.stat()
                records.append(
                    {
                        "kind": kind,
                        "path": str(resolved),
                        "exists": True,
                        "size": state.st_size,
                        "modified_ns": state.st_mtime_ns,
                    }
                )
            except OSError:
                records.append({"kind": kind, "path": str(resolved), "exists": False})
    return sorted(records, key=lambda item: (item["kind"], item["path"]))


def _environment(run: dict) -> dict:
    return {
        "scenetrace_version": TOOL_VERSION,
        "blender_version": list(bpy.app.version),
        "blender_version_string": bpy.app.version_string,
        "operating_system": platform.system(),
        "architecture": platform.machine(),
        "startup_mode": "factory",
        "measurement_mode": run.get("measurement_mode"),
        "benchmark_settings": run.get("settings", {}),
        "renderer": bpy.context.scene.render.engine,
    }


def _baseline_run(profile: dict) -> dict:
    return profile.get("aggregate", profile)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scenetrace-headless")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--frame-start", type=int)
        p.add_argument("--frame-end", type=int)
        p.add_argument("--frame-step", type=int, default=1)
        p.add_argument("--repetitions", type=int, default=3)
        p.add_argument("--warmups", type=int, default=1)
        p.add_argument("--no-modifier-timings", action="store_true")
        p.add_argument("--output", type=Path, required=True)

    baseline = sub.add_parser("baseline")
    common(baseline)
    baseline.add_argument("--calibration-runs", type=int, default=4)

    test = sub.add_parser("test")
    common(test)
    test.add_argument("--baseline", type=Path, required=True)
    test.add_argument("--threshold-percent", type=float, default=20.0)
    test.add_argument("--min-delta-ms", type=float, default=2.0)

    return parser


def _settings(args) -> tuple[int, int, int, int, int, bool]:
    scene = bpy.context.scene
    start = scene.frame_start if args.frame_start is None else int(args.frame_start)
    end = scene.frame_end if args.frame_end is None else int(args.frame_end)
    step = max(1, int(args.frame_step))
    repetitions = max(1, int(args.repetitions))
    warmups = max(0, int(args.warmups))
    capture_modifiers = not bool(args.no_modifier_timings)
    if end < start:
        raise ValueError(f"Invalid frame range: {start}..{end}")
    return start, end, step, repetitions, warmups, capture_modifiers


def _run_once(args) -> dict:
    start, end, step, repetitions, warmups, capture_modifiers = _settings(args)
    run = run_benchmark(
        bpy.context,
        start,
        end,
        step,
        repetitions,
        warmups,
        capture_modifiers,
    )
    run["environment"] = _environment(run)
    run["dependencies"] = _dependency_records()
    return run


def command_baseline(args) -> int:
    runs = [_run_once(args) for _ in range(max(1, int(args.calibration_runs)))]
    profile = build_baseline_profile(runs)
    profile["tool_version"] = TOOL_VERSION
    profile["schema_version"] = ARTIFACT_SCHEMA_VERSION
    profile["scenetrace_version"] = TOOL_VERSION
    profile["created_at"] = datetime.now(timezone.utc).isoformat()
    profile["environment"] = runs[-1]["environment"]
    profile["dependencies"] = runs[-1]["dependencies"]
    profile["benchmark_environment"] = "background"
    _save(args.output, profile)
    summary = profile["aggregate"]["summary"]
    noise = profile.get("noise", {}).get("p95", {})
    print(
        "SCENETRACE_HEADLESS_BASELINE "
        f"p95={summary.get('p95_ms', 0.0):.6f} "
        f"noise_pct={noise.get('percent', 0.0):.6f} "
        f"runs={profile.get('calibration_runs', 0)}"
    )
    return 0


def command_test(args) -> int:
    baseline = _json(args.baseline)
    run = _run_once(args)
    comparison = compare_runs(
        baseline,
        run,
        float(args.threshold_percent),
        float(args.min_delta_ms),
    )
    base_run = _baseline_run(baseline)
    changes = diff_snapshots(
        base_run.get("scene_snapshot", {}),
        run.get("scene_snapshot", {}),
        limit=100,
    )
    comparison["diagnosis"] = build_diagnosis(
        baseline,
        run,
        changes,
        comparison.get("modifier_timing_signals", []),
    )
    payload = {
        "schema": "scenetrace-headless-report",
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "version": 1,
        "tool_version": TOOL_VERSION,
        "scenetrace_version": TOOL_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_environment": "background",
        "environment": run["environment"],
        "dependencies": run["dependencies"],
        "run": run,
        "comparison": comparison,
        "correlated_changes": changes,
    }
    _save(args.output, payload)
    print(
        "SCENETRACE_HEADLESS_TEST "
        f"p95={run.get('summary', {}).get('p95_ms', 0.0):.6f} "
        f"failed={str(bool(comparison.get('failed'))).lower()}"
    )
    # Blender itself exits successfully when measurement completed. The Rust
    # parent owns policy/CI exit codes so a performance regression is not
    # confused with a Blender execution failure.
    return 0


def main(argv: list[str] | None = None) -> int:
    if not bpy.app.background:
        print("SceneTrace headless runner must be launched with Blender --background", file=sys.stderr)
        return 2
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "baseline":
            return command_baseline(args)
        if args.command == "test":
            return command_test(args)
        return 2
    except Exception as exc:
        print(f"SCENETRACE_HEADLESS_ERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    # Everything after Blender's `--` belongs to SceneTrace.
    raw = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    raise SystemExit(main(raw))
