# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import platform
import time
from collections import defaultdict
from datetime import datetime, timezone

import bpy

from .analysis import aggregate_modifier_repetitions, aggregate_repetitions, summarize, summarize_modifiers
from .snapshot import build_scene_snapshot


def _capture_modifier_times(scene, depsgraph) -> list[dict]:
    """Read Blender's evaluated Modifier.execution_time after a depsgraph update.

    These are Blender-reported measurements, but they must not be summed as a
    decomposition of frame wall time: Blender notes that parallel modifier
    evaluation makes execution_time non-additive/unreliable in that case.
    """
    rows = []
    for obj in scene.objects:
        try:
            evaluated = obj.evaluated_get(depsgraph)
        except Exception:
            continue
        try:
            modifiers = evaluated.modifiers
        except Exception:
            continue
        for mod in modifiers:
            try:
                seconds = float(mod.execution_time)
            except Exception:
                continue
            if seconds <= 0.0:
                continue
            rows.append({
                "object": obj.name,
                "modifier": mod.name,
                "type": mod.type,
                "ms": seconds * 1000.0,
            })
    return rows


def run_benchmark(
    context,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    repetitions: int,
    warmups: int,
    capture_modifier_timings: bool = True,
) -> dict:
    scene = context.scene
    frames = list(range(frame_start, frame_end + 1, max(1, frame_step)))
    if not frames:
        raise ValueError("The frame range is empty")

    original_frame = scene.frame_current
    depsgraph = context.evaluated_depsgraph_get()
    samples: dict[int, list[float]] = {frame: [] for frame in frames}
    modifier_samples: dict[tuple, list[float]] = defaultdict(list)
    wm = getattr(context, "window_manager", None)
    total = max(1, (warmups + repetitions) * len(frames))
    progress = 0
    progress_enabled = False
    if wm is not None:
        try:
            wm.progress_begin(0, total)
            progress_enabled = True
        except Exception:
            progress_enabled = False
    scene_snapshot = None

    try:
        for _ in range(warmups):
            for frame in frames:
                scene.frame_set(frame)
                context.view_layer.update()
                depsgraph.update()
                progress += 1
                if progress_enabled:
                    try:
                        wm.progress_update(progress)
                    except Exception:
                        progress_enabled = False

        for repeat in range(repetitions):
            ordered = frames if repeat % 2 == 0 else list(reversed(frames))
            for frame in ordered:
                start = time.perf_counter_ns()
                scene.frame_set(frame)
                context.view_layer.update()
                depsgraph.update()
                elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000.0
                samples[frame].append(elapsed_ms)

                # Read attribution only after stopping the wall-time clock so the
                # bookkeeping itself is not counted as frame evaluation time.
                if capture_modifier_timings:
                    for row in _capture_modifier_times(scene, depsgraph):
                        key = (frame, row["object"], row["modifier"], row["type"])
                        modifier_samples[key].append(float(row["ms"]))

                progress += 1
                if progress_enabled:
                    try:
                        wm.progress_update(progress)
                    except Exception:
                        progress_enabled = False

        # Structural attribution must always be captured at a deterministic
        # reference frame. Otherwise two identical scenes can look different
        # simply because the user happened to be parked on different frames.
        scene.frame_set(frame_start)
        context.view_layer.update()
        depsgraph.update()
        scene_snapshot = build_scene_snapshot(scene, depsgraph)
    finally:
        scene.frame_set(original_frame)
        context.view_layer.update()
        if progress_enabled:
            try:
                wm.progress_end()
            except Exception:
                pass

    aggregated = aggregate_repetitions(samples)
    modifier_timings = aggregate_modifier_repetitions(modifier_samples)
    return {
        "version": 7,
        "tool_version": "1.0.1",
        "measurement_mode": "depsgraph_frame_update_wall_time",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "blend_file": bpy.data.filepath or "<unsaved>",
        "blender_version": bpy.app.version_string,
        "platform": platform.platform(),
        "settings": {
            "frame_start": frame_start,
            "frame_end": frame_end,
            "frame_step": frame_step,
            "repetitions": repetitions,
            "warmups": warmups,
            "capture_modifier_timings": capture_modifier_timings,
            "snapshot_reference_frame": frame_start,
            "execution_mode": "background" if bpy.app.background else "interactive",
        },
        "samples": aggregated,
        "summary": summarize(aggregated),
        "modifier_timings": modifier_timings,
        "modifier_summary": summarize_modifiers(modifier_timings),
        "scene_snapshot": scene_snapshot or {},
    }
