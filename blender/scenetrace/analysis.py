# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import math
from collections import defaultdict
from statistics import median


TOOL_VERSION = "1.0.2"


def compute_graph_layout(
    region_width: int,
    region_height: int,
    left_inset: int = 0,
    right_inset: int = 0,
    position: str = "TOP",
) -> dict | None:
    """Return a responsive, UI-safe graph rectangle in WINDOW-region coordinates.

    ``left_inset`` and ``right_inset`` represent overlapping Blender UI regions
    such as the T toolbar and N sidebar. The function is intentionally pure so
    the layout policy can be regression-tested without importing ``bpy``.
    """
    rw = max(0, int(region_width))
    rh = max(0, int(region_height))
    left = max(0, int(left_inset)) + 16
    right = max(0, int(right_inset)) + 16
    usable_w = rw - left - right
    if usable_w < 260 or rh < 180:
        return None

    width = min(720, usable_w)
    height = min(220, max(160, int(rh * 0.30)))
    x0 = left + max(0, (usable_w - width) // 2)

    vertical_margin = 28
    if str(position).upper() == "BOTTOM":
        y0 = vertical_margin
    else:
        y0 = max(vertical_margin, rh - height - vertical_margin)

    # Keep the graph inside the drawable WINDOW region even on short layouts.
    if y0 + height > rh - 8:
        y0 = max(8, rh - height - 8)
    return {
        "x": float(x0),
        "y": float(y0),
        "width": float(width),
        "height": float(height),
        "compact_labels": width < 500,
    }


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    w = pos - lo
    return ordered[lo] * (1.0 - w) + ordered[hi] * w


def summarize(samples: list[dict]) -> dict:
    if not samples:
        return {"median_ms": 0.0, "p95_ms": 0.0, "worst_ms": 0.0, "worst_frame": 0}
    values = [float(item["ms"]) for item in samples]
    worst = max(samples, key=lambda item: float(item["ms"]))
    return {
        "median_ms": percentile(values, 0.5),
        "p95_ms": percentile(values, 0.95),
        "worst_ms": float(worst["ms"]),
        "worst_frame": int(worst["frame"]),
    }


def _pct(base: float, current: float) -> float:
    if abs(base) < 1e-9:
        return 0.0 if abs(current) < 1e-9 else 100.0
    return (current - base) / base * 100.0


def frame_budget(summary: dict, target_fps: int) -> dict:
    """Relate measured scene-evaluation time to a target frame budget.

    SceneTrace measures dependency-graph evaluation wall time, not full viewport
    presentation/render time. Therefore passing this budget is necessary but not
    sufficient for actually achieving the target FPS.
    """
    fps = max(1, int(target_fps or 1))
    budget_ms = 1000.0 / fps
    p95_ms = float(summary.get("p95_ms", 0.0))
    utilization = (p95_ms / budget_ms * 100.0) if budget_ms > 0 else 0.0
    remaining = budget_ms - p95_ms
    return {
        "target_fps": fps,
        "budget_ms": budget_ms,
        "p95_ms": p95_ms,
        "utilization_percent": utilization,
        "remaining_ms": remaining,
        "passes": p95_ms <= budget_ms,
        "max_theoretical_fps_from_evaluation": (1000.0 / p95_ms) if p95_ms > 1e-9 else None,
        "note": (
            "SceneTrace measures scene evaluation only. Viewport drawing, GPU work, compositing and display overhead are not included."
        ),
    }


def build_timeline(baseline: dict, current: dict, max_points: int = 16) -> list[dict]:
    """Build compact baseline/current timeline buckets for the Blender UI.

    Buckets use the worst current frame in each segment so localized spikes are
    preserved when long animation ranges are downsampled for the sidebar.
    """
    base_run = _baseline_run(baseline)
    base_by_frame = {int(item["frame"]): float(item["ms"]) for item in base_run.get("samples", [])}
    cur_by_frame = {int(item["frame"]): float(item["ms"]) for item in current.get("samples", [])}
    frames = sorted(set(base_by_frame) & set(cur_by_frame))
    if not frames:
        return []
    max_points = max(1, int(max_points))
    bucket_size = max(1, math.ceil(len(frames) / max_points))
    buckets = []
    for offset in range(0, len(frames), bucket_size):
        group = frames[offset:offset + bucket_size]
        representative = max(group, key=lambda f: cur_by_frame[f])
        buckets.append({
            "frame_start": group[0],
            "frame_end": group[-1],
            "frame": representative,
            "baseline_ms": base_by_frame[representative],
            "current_ms": cur_by_frame[representative],
            "delta_ms": cur_by_frame[representative] - base_by_frame[representative],
            "delta_percent": _pct(base_by_frame[representative], cur_by_frame[representative]),
        })
    scale = max((max(item["baseline_ms"], item["current_ms"]) for item in buckets), default=1.0)
    scale = max(scale, 1e-9)
    for item in buckets:
        item["baseline_ratio"] = item["baseline_ms"] / scale
        item["current_ratio"] = item["current_ms"] / scale
    return buckets



def build_graph_series(baseline: dict, current: dict, target_fps: int = 30, max_points: int = 120) -> dict:
    """Return line-graph data for the viewport performance overlay.

    Long ranges are downsampled using the worst current frame in each bucket so
    short regressions stay visible instead of being averaged away. The graph is
    a visualization of SceneTrace's evaluation-time measurement only.
    """
    base_run = _baseline_run(baseline)
    base_by_frame = {int(item["frame"]): float(item["ms"]) for item in base_run.get("samples", [])}
    cur_by_frame = {int(item["frame"]): float(item["ms"]) for item in current.get("samples", [])}
    frames = sorted(set(base_by_frame) & set(cur_by_frame))
    if not frames:
        return {"points": [], "budget_ms": 1000.0 / max(1, int(target_fps or 1)), "y_max_ms": 1.0}

    max_points = max(2, int(max_points))
    bucket_size = max(1, math.ceil(len(frames) / max_points))
    chosen = []
    for offset in range(0, len(frames), bucket_size):
        group = frames[offset:offset + bucket_size]
        # Preserve the most expensive current point in the bucket. If current is
        # flat this still produces a stable chronological representative.
        representative = max(group, key=lambda f: cur_by_frame[f])
        chosen.append(representative)

    # Keep chronological order and force both endpoints into the graph when
    # downsampling otherwise selected a nearby spike instead.
    if frames[0] not in chosen:
        chosen.insert(0, frames[0])
    if frames[-1] not in chosen:
        chosen.append(frames[-1])
    chosen = sorted(set(chosen))

    points = [
        {
            "frame": frame,
            "baseline_ms": base_by_frame[frame],
            "current_ms": cur_by_frame[frame],
        }
        for frame in chosen
    ]
    budget_ms = 1000.0 / max(1, int(target_fps or 1))
    max_observed = max(
        [budget_ms]
        + [point["baseline_ms"] for point in points]
        + [point["current_ms"] for point in points]
    )
    return {
        "points": points,
        "frame_start": frames[0],
        "frame_end": frames[-1],
        "budget_ms": budget_ms,
        "target_fps": max(1, int(target_fps or 1)),
        "y_max_ms": max(1.0, max_observed * 1.12),
    }


def build_product_status(baseline: dict, current: dict, comparison: dict, target_fps: int = 30) -> dict:
    """Build the concise, user-facing diagnosis shown before technical evidence."""
    base_run = _baseline_run(baseline)
    base_p95 = float(base_run.get("summary", {}).get("p95_ms", 0.0))
    current_p95 = float(current.get("summary", {}).get("p95_ms", 0.0))
    noise_pct = float(comparison.get("expected_noise", {}).get("p95", {}).get("percent", 0.0))
    threshold_pct = float(comparison.get("effective_p95_threshold_percent", 0.0))
    delta_pct = float(comparison.get("p95_delta_percent", 0.0))
    failed = bool(comparison.get("failed"))
    pattern = comparison.get("pattern", {})
    diagnosis = comparison.get("diagnosis", {})
    summary = diagnosis.get("summary") or {}
    budget = frame_budget(current.get("summary", {}), target_fps)

    if failed:
        state = "regression"
        title = "PERFORMANCE REGRESSION"
        if summary.get("modifier") and summary.get("object"):
            primary = f"{summary['modifier']} on {summary['object']}"
        else:
            primary = summary.get("headline") or "Performance changed significantly"
        affected = int(pattern.get("affected_frames", 0))
        total = int(pattern.get("total_frames", 0))
        pattern_text = pattern.get("kind", "unknown").upper()
        if total:
            pattern_text += f" · {affected}/{total} frames"
    else:
        state = "stable"
        title = "PERFORMANCE STABLE"
        direction = "faster" if delta_pct < -0.5 else "slower" if delta_pct > 0.5 else "unchanged"
        primary = f"P95 is {abs(delta_pct):.1f}% {direction}" if direction != "unchanged" else "P95 is effectively unchanged"
        pattern_text = "Within learned baseline variance" if noise_pct > 0 else "Within regression threshold"

    signal_to_threshold = (abs(delta_pct) / threshold_pct) if threshold_pct > 1e-9 else None
    return {
        "state": state,
        "title": title,
        "baseline_p95_ms": base_p95,
        "current_p95_ms": current_p95,
        "delta_percent": delta_pct,
        "noise_percent": noise_pct,
        "effective_threshold_percent": threshold_pct,
        "confidence": comparison.get("confidence", "STABLE"),
        "primary": primary,
        "pattern_text": pattern_text,
        "signal_to_threshold": signal_to_threshold,
        "diagnosis": summary,
        "budget": budget,
    }

def _absolute_relative_deviation(values: list[float], center: float) -> list[float]:
    if abs(center) < 1e-9:
        return [0.0 for _ in values]
    return [abs(v - center) / abs(center) * 100.0 for v in values]


def _noise(values: list[float], center: float) -> dict:
    if len(values) < 2:
        return {"percent": 0.0, "ms": 0.0, "samples": len(values)}
    absolute_ms = [abs(v - center) for v in values]
    relative = _absolute_relative_deviation(values, center)
    return {
        "percent": percentile(relative, 0.95),
        "ms": percentile(absolute_ms, 0.95),
        "samples": len(values),
    }


def _noise_quality(calibration_runs: int, median_noise: float, p95_noise: float) -> str:
    if calibration_runs < 3:
        return "INSUFFICIENT"
    worst = max(median_noise, p95_noise)
    if worst <= 5.0:
        return "EXCELLENT"
    if worst <= 10.0:
        return "GOOD"
    if worst <= 20.0:
        return "NOISY"
    return "UNSTABLE"


def aggregate_repetitions(per_frame: dict[int, list[float]]) -> list[dict]:
    return [
        {"frame": frame, "ms": median(values), "samples_ms": list(values)}
        for frame, values in sorted(per_frame.items())
    ]


def aggregate_modifier_repetitions(per_modifier: dict[tuple, list[float]]) -> list[dict]:
    items = []
    for (frame, object_name, modifier_name, modifier_type), values in sorted(per_modifier.items()):
        items.append({
            "frame": int(frame),
            "object": object_name,
            "modifier": modifier_name,
            "type": modifier_type,
            "ms": median(values),
            "samples_ms": list(values),
        })
    return items


def summarize_modifiers(timings: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for item in timings:
        grouped[(item["object"], item["modifier"], item.get("type", ""))].append(item)

    result = []
    for (object_name, modifier_name, modifier_type), items in grouped.items():
        samples = [{"frame": i["frame"], "ms": i["ms"]} for i in items]
        s = summarize(samples)
        result.append({
            "object": object_name,
            "modifier": modifier_name,
            "type": modifier_type,
            **s,
        })
    result.sort(key=lambda item: item["p95_ms"], reverse=True)
    return result


def build_baseline_profile(runs: list[dict]) -> dict:
    if not runs:
        raise ValueError("At least one benchmark run is required")

    by_frame: dict[int, list[float]] = defaultdict(list)
    for run in runs:
        for item in run.get("samples", []):
            by_frame[int(item["frame"])].append(float(item["ms"]))

    aggregate_samples = []
    frame_noise = {}
    for frame, values in sorted(by_frame.items()):
        center = median(values)
        aggregate_samples.append({"frame": frame, "ms": center, "calibration_ms": values})
        frame_noise[str(frame)] = _noise(values, center)

    aggregate_summary = summarize(aggregate_samples)
    median_values = [float(run["summary"]["median_ms"]) for run in runs]
    p95_values = [float(run["summary"]["p95_ms"]) for run in runs]
    worst_values = [float(run["summary"]["worst_ms"]) for run in runs]
    median_noise = _noise(median_values, aggregate_summary["median_ms"])
    p95_noise = _noise(p95_values, aggregate_summary["p95_ms"])
    worst_noise = _noise(worst_values, aggregate_summary["worst_ms"])

    # Aggregate Blender-reported evaluated modifier timings across calibration runs.
    modifier_values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for run in runs:
        for item in run.get("modifier_summary", []):
            key = (item["object"], item["modifier"], item.get("type", ""))
            modifier_values[key].append(float(item["p95_ms"]))
    modifier_baseline = []
    modifier_noise = {}
    for key, values in modifier_values.items():
        center = median(values)
        object_name, modifier_name, modifier_type = key
        modifier_baseline.append({
            "object": object_name,
            "modifier": modifier_name,
            "type": modifier_type,
            "p95_ms": center,
        })
        modifier_noise["|".join(key)] = _noise(values, center)
    modifier_baseline.sort(key=lambda item: item["p95_ms"], reverse=True)

    latest = runs[-1]
    aggregate_run = {
        **latest,
        "samples": aggregate_samples,
        "summary": aggregate_summary,
        "modifier_summary": modifier_baseline,
    }

    quality = _noise_quality(len(runs), median_noise["percent"], p95_noise["percent"])
    return {
        "schema": "scenetrace-baseline",
        "version": 4,
        "tool_version": TOOL_VERSION,
        "calibration_runs": len(runs),
        "aggregate": aggregate_run,
        "noise": {
            "median": median_noise,
            "p95": p95_noise,
            "worst": worst_noise,
            "quality": quality,
        },
        "frame_noise": frame_noise,
        "modifier_noise": modifier_noise,
        "run_summaries": [run.get("summary", {}) for run in runs],
    }


def _baseline_run(baseline: dict) -> dict:
    if baseline.get("schema") == "scenetrace-baseline" and baseline.get("aggregate"):
        return baseline["aggregate"]
    return baseline


def _baseline_noise(baseline: dict) -> dict:
    if baseline.get("schema") == "scenetrace-baseline":
        return baseline.get("noise", {})
    return {}


def _comparison_warnings(baseline: dict, current: dict) -> list[str]:
    base_run = _baseline_run(baseline)
    warnings = []
    if base_run.get("measurement_mode") and base_run.get("measurement_mode") != current.get("measurement_mode"):
        warnings.append("Measurement mode differs from baseline")
    base_mode = base_run.get("settings", {}).get("execution_mode")
    cur_mode = current.get("settings", {}).get("execution_mode")
    if base_mode and cur_mode and base_mode != cur_mode:
        warnings.append(f"Execution mode differs: {base_mode} → {cur_mode}; timings should not be compared")
    if base_run.get("blender_version") and base_run.get("blender_version") != current.get("blender_version"):
        warnings.append(f"Blender version changed: {base_run.get('blender_version')} → {current.get('blender_version')}")
    if base_run.get("platform") and base_run.get("platform") != current.get("platform"):
        warnings.append("Operating system/hardware platform string differs from baseline")
    base_settings = base_run.get("settings", {})
    cur_settings = current.get("settings", {})
    for key in ("frame_start", "frame_end", "frame_step"):
        if key in base_settings and key in cur_settings and base_settings[key] != cur_settings[key]:
            warnings.append(f"Benchmark {key.replace('_', ' ')} differs: {base_settings[key]} → {cur_settings[key]}")
    base_snapshot = base_run.get("scene_snapshot", {})
    cur_snapshot = current.get("scene_snapshot", {})
    if base_snapshot.get("reference_frame") is None:
        warnings.append("Baseline predates deterministic attribution snapshots; recalibrate for cleaner change diagnosis")
    elif cur_snapshot.get("reference_frame") is not None and base_snapshot.get("reference_frame") != cur_snapshot.get("reference_frame"):
        warnings.append(
            f"Attribution reference frame differs: {base_snapshot.get('reference_frame')} → {cur_snapshot.get('reference_frame')}"
        )
    if baseline.get("schema") != "scenetrace-baseline":
        warnings.append("Legacy/single-run baseline: noise floor is unknown")
    elif int(baseline.get("calibration_runs", 0)) < 3:
        warnings.append("Baseline has fewer than 3 calibration runs; noise estimate is weak")
    return warnings


def environment_compatibility(baseline: dict, current: dict) -> dict:
    baseline_run = _baseline_run(baseline)
    baseline_environment = baseline.get("environment") or baseline_run.get("environment") or {}
    current_environment = current.get("environment") or {}
    if not baseline_environment or not current_environment:
        return {
            "status": "warning",
            "reasons": ["Environment fingerprint missing; comparison is less auditable"],
        }

    incompatible = []
    warnings = []
    baseline_blender = baseline_environment.get("blender_version", [])
    current_blender = current_environment.get("blender_version", [])
    if list(baseline_blender[:2]) != list(current_blender[:2]):
        incompatible.append(
            f"Blender measurement version differs: {baseline_blender} -> {current_blender}"
        )
    for key, label in (
        ("startup_mode", "startup mode"),
        ("measurement_mode", "measurement mode"),
        ("benchmark_settings", "benchmark settings"),
    ):
        if baseline_environment.get(key) != current_environment.get(key):
            incompatible.append(f"Benchmark {label} differs")
    for key, label in (("operating_system", "operating system"), ("architecture", "architecture")):
        if baseline_environment.get(key) != current_environment.get(key):
            warnings.append(f"Benchmark {label} differs")

    if incompatible:
        return {"status": "incompatible", "reasons": incompatible + warnings}
    if warnings:
        return {"status": "warning", "reasons": warnings}
    return {"status": "compatible", "reasons": []}


def _ranges(frames: list[int], step: int) -> list[dict]:
    if not frames:
        return []
    ordered = sorted(set(frames))
    ranges = []
    start = prev = ordered[0]
    for frame in ordered[1:]:
        if frame - prev <= max(1, step):
            prev = frame
            continue
        ranges.append({"start": start, "end": prev})
        start = prev = frame
    ranges.append({"start": start, "end": prev})
    return ranges


def classify_pattern(regressed_frames: list[dict], total_frames: int, frame_step: int = 1) -> dict:
    count = len(regressed_frames)
    percent = (count / total_frames * 100.0) if total_frames else 0.0
    if count == 0:
        kind = "none"
    elif percent >= 70.0:
        kind = "persistent"
    elif percent >= 25.0:
        kind = "widespread"
    else:
        kind = "localized"
    return {
        "kind": kind,
        "affected_frames": count,
        "total_frames": total_frames,
        "affected_percent": percent,
        "ranges": _ranges([int(i["frame"]) for i in regressed_frames], frame_step),
    }


def compare_modifier_timings(
    baseline: dict,
    current: dict,
    threshold_percent: float,
    min_delta_ms: float,
) -> list[dict]:
    """Return measured modifier timing signals worth surfacing.

    Existing modifiers are only returned when they regress beyond their own
    learned noise floor. Newly added modifiers have no baseline by definition,
    so they are returned when Blender reports a non-trivial current P95 time.
    """
    base_run = _baseline_run(baseline)
    base_map = {
        (i["object"], i["modifier"], i.get("type", "")): float(i.get("p95_ms", 0.0))
        for i in base_run.get("modifier_summary", [])
    }
    current_map = {
        (i["object"], i["modifier"], i.get("type", "")): float(i.get("p95_ms", 0.0))
        for i in current.get("modifier_summary", [])
    }
    modifier_noise = baseline.get("modifier_noise", {}) if baseline.get("schema") == "scenetrace-baseline" else {}
    results = []
    min_signal_ms = min(0.5, max(0.05, min_delta_ms / 4.0))

    for key, cur in current_map.items():
        if key not in base_map:
            if cur >= min_signal_ms:
                results.append({
                    "status": "new",
                    "object": key[0],
                    "modifier": key[1],
                    "type": key[2],
                    "baseline_p95_ms": None,
                    "current_p95_ms": cur,
                    "delta_ms": cur,
                    "delta_percent": None,
                    "effective_threshold_percent": None,
                })
            continue

        base = base_map[key]
        delta = cur - base
        pct = _pct(base, cur)
        noise_pct = float(modifier_noise.get("|".join(key), {}).get("percent", 0.0))
        effective = max(threshold_percent, noise_pct * 2.0)
        if delta >= min_signal_ms and pct >= effective:
            results.append({
                "status": "regressed",
                "object": key[0],
                "modifier": key[1],
                "type": key[2],
                "baseline_p95_ms": base,
                "current_p95_ms": cur,
                "delta_ms": delta,
                "delta_percent": pct,
                "effective_threshold_percent": effective,
            })

    # Biggest observed delta first. This intentionally does not sum timings.
    results.sort(key=lambda i: (float(i.get("delta_ms", 0.0)), float(i.get("current_p95_ms", 0.0))), reverse=True)
    return results


def _change_priority(change: dict) -> int:
    try:
        return int(change.get("priority", 0))
    except Exception:
        return 0


def _primary_label(group: dict) -> str:
    timings = group.get("timing_signals", [])
    if timings:
        top = timings[0]
        if top.get("status") == "new":
            return f"New {top.get('modifier', 'modifier')} · {top.get('current_p95_ms', 0.0):.2f} ms P95"
        return f"{top.get('modifier', 'Modifier')} timing +{top.get('delta_ms', 0.0):.2f} ms"

    changes = group.get("changes", [])
    baked = next((c for c in changes if c.get("kind") == "possible_modifier_applied"), None)
    if baked:
        return baked.get("label", "Likely baked geometry")
    geometry = [c for c in changes if c.get("kind") == "geometry"]
    if geometry:
        return "Mesh complexity changed"
    if changes:
        return changes[0].get("label", "Scene changed")
    return "Changed object"


def build_diagnosis(
    baseline: dict,
    current: dict,
    changes: list[dict],
    timing_signals: list[dict] | None = None,
) -> dict:
    """Combine measured timing signals and structural changes into ranked clues.

    The result is intentionally an evidence ranking, not causal attribution.
    Modifier execution_time can overlap under parallel evaluation, so the
    coverage estimate uses only the single largest measured positive signal.
    """
    timing_signals = list(timing_signals or [])
    base_run = _baseline_run(baseline)
    base_p95 = float(base_run.get("summary", {}).get("p95_ms", 0.0))
    current_p95 = float(current.get("summary", {}).get("p95_ms", 0.0))
    frame_delta = max(0.0, current_p95 - base_p95)

    groups: dict[str, dict] = {}

    def group_for(name: str) -> dict:
        if name not in groups:
            groups[name] = {"object": name, "changes": [], "timing_signals": []}
        return groups[name]

    for change in changes:
        entity = str(change.get("entity", "<scene>"))
        group_for(entity)["changes"].append(change)
    for signal in timing_signals:
        entity = str(signal.get("object", "<scene>"))
        group_for(entity)["timing_signals"].append(signal)

    contributors = []
    for name, group in groups.items():
        group["changes"].sort(key=lambda c: (-_change_priority(c), c.get("label", "")))
        group["timing_signals"].sort(key=lambda s: float(s.get("delta_ms", 0.0)), reverse=True)

        max_change_priority = max((_change_priority(c) for c in group["changes"]), default=0)
        strongest_timing = max((float(s.get("delta_ms", 0.0)) for s in group["timing_signals"]), default=0.0)
        has_measured = strongest_timing > 0.0
        has_structural = bool(group["changes"])
        possible_baked = any(c.get("kind") == "possible_modifier_applied" for c in group["changes"])
        geometry_changes = [c for c in group["changes"] if c.get("kind") == "geometry"]
        new_modifier = any(s.get("status") == "new" for s in group["timing_signals"])

        # Transform-only / low-priority snapshot differences are not useful
        # performance-attribution candidates. They are intentionally omitted
        # unless Blender also measured a timing signal on the object.
        if not has_measured and max_change_priority < 50:
            continue

        score = min(65.0, max_change_priority * 0.60)
        if has_measured:
            timing_ratio = strongest_timing / max(frame_delta, 0.1)
            score = max(score, 62.0 + min(23.0, timing_ratio * 23.0))
        if new_modifier:
            score += 6.0
        if possible_baked:
            score += 15.0
        if geometry_changes:
            max_growth = max((abs(float(c.get("delta_percent", 0.0))) for c in geometry_changes), default=0.0)
            score += min(10.0, max_growth / 100.0)
        if has_measured and has_structural:
            score += 8.0
        score = min(100.0, score)

        if has_measured and has_structural and score >= 75.0:
            confidence = "HIGH"
        elif has_measured or max_change_priority >= 80:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        contributors.append({
            "object": name,
            "score": round(score, 1),
            "confidence": confidence,
            "headline": _primary_label(group),
            "measured_delta_ms": strongest_timing,
            "timing_signals": group["timing_signals"][:4],
            "changes": group["changes"][:6],
        })

    contributors.sort(
        key=lambda g: (float(g.get("score", 0.0)), float(g.get("measured_delta_ms", 0.0))),
        reverse=True,
    )

    largest = max((float(s.get("delta_ms", 0.0)) for s in timing_signals), default=0.0)
    conservative_covered = min(frame_delta, max(0.0, largest)) if frame_delta > 0.0 else 0.0
    remaining = max(0.0, frame_delta - conservative_covered)
    coverage_percent = conservative_covered / frame_delta * 100.0 if frame_delta > 1e-9 else 0.0

    top = contributors[0] if contributors else None
    diagnosis_summary = None
    if top:
        top_signal = (top.get("timing_signals") or [None])[0]
        top_geometry = next(
            (c for c in top.get("changes", []) if c.get("kind") == "evaluated_geometry" and c.get("label") in {"Evaluated Vertices", "Evaluated Triangles"}),
            None,
        )
        diagnosis_summary = {
            "object": top.get("object"),
            "confidence": top.get("confidence", "LOW"),
            "score": top.get("score", 0),
            "modifier": top_signal.get("modifier") if top_signal else None,
            "modifier_status": top_signal.get("status") if top_signal else None,
            "measured_delta_ms": float(top.get("measured_delta_ms", 0.0)),
            "headline": top.get("headline", "Likely contributor"),
            "coverage_percent": coverage_percent,
            "geometry_label": top_geometry.get("label") if top_geometry else None,
            "geometry_before": top_geometry.get("before") if top_geometry else None,
            "geometry_after": top_geometry.get("after") if top_geometry else None,
            "geometry_delta_percent": top_geometry.get("delta_percent") if top_geometry else None,
        }

    return {
        "frame_p95_baseline_ms": base_p95,
        "frame_p95_current_ms": current_p95,
        "frame_p95_delta_ms": current_p95 - base_p95,
        "contributors": contributors,
        "summary": diagnosis_summary,
        "coverage": {
            "method": "largest_single_modifier_signal",
            "largest_measured_modifier_delta_ms": largest,
            "conservative_covered_ms": conservative_covered,
            "remaining_ms": remaining,
            "coverage_percent": coverage_percent,
            "note": (
                "Conservative coverage uses only the largest measured modifier delta because Blender "
                "modifier execution times may overlap under parallel evaluation. It is not an additive cost breakdown."
            ),
        },
    }


def compare_runs(baseline: dict, current: dict, threshold_percent: float, min_delta_ms: float) -> dict:
    base_run = _baseline_run(baseline)
    base_by_frame = {int(item["frame"]): float(item["ms"]) for item in base_run.get("samples", [])}
    frame_noise = baseline.get("frame_noise", {}) if baseline.get("schema") == "scenetrace-baseline" else {}
    regressed = []
    common_frames = 0
    for item in current.get("samples", []):
        frame = int(item["frame"])
        if frame not in base_by_frame:
            continue
        common_frames += 1
        base = base_by_frame[frame]
        cur = float(item["ms"])
        delta = cur - base
        delta_percent = _pct(base, cur)
        learned = frame_noise.get(str(frame), {})
        effective_percent = max(threshold_percent, float(learned.get("percent", 0.0)) * 2.0)
        effective_ms = max(min_delta_ms, float(learned.get("ms", 0.0)) * 2.0)
        if delta >= effective_ms and delta_percent >= effective_percent:
            regressed.append({
                "frame": frame,
                "baseline_ms": base,
                "current_ms": cur,
                "delta_ms": delta,
                "delta_percent": delta_percent,
                "effective_threshold_percent": effective_percent,
                "effective_threshold_ms": effective_ms,
            })
    regressed.sort(key=lambda item: item["delta_ms"], reverse=True)

    b = base_run["summary"]
    c = current["summary"]
    median_delta = _pct(float(b["median_ms"]), float(c["median_ms"]))
    p95_delta = _pct(float(b["p95_ms"]), float(c["p95_ms"]))
    worst_delta = _pct(float(b["worst_ms"]), float(c["worst_ms"]))
    p95_delta_ms = float(c["p95_ms"]) - float(b["p95_ms"])

    noise = _baseline_noise(baseline)
    learned_p95_noise = float(noise.get("p95", {}).get("percent", 0.0))
    learned_p95_noise_ms = float(noise.get("p95", {}).get("ms", 0.0))
    effective_p95_threshold = max(threshold_percent, learned_p95_noise * 2.0)
    effective_p95_ms = max(min_delta_ms, learned_p95_noise_ms * 2.0)

    step = int(current.get("settings", {}).get("frame_step", 1))
    pattern = classify_pattern(regressed, common_frames, step)
    environment = environment_compatibility(baseline, current)
    failed = (
        environment["status"] != "incompatible"
        and ((p95_delta >= effective_p95_threshold and p95_delta_ms >= effective_p95_ms) or bool(regressed))
    )

    if not failed:
        confidence = "STABLE"
    else:
        signal_ratio = p95_delta / effective_p95_threshold if effective_p95_threshold > 0 else 99.0
        confidence = "HIGH" if signal_ratio >= 2.0 or pattern["affected_percent"] >= 50.0 else "MEDIUM"

    timing_signals = compare_modifier_timings(baseline, current, threshold_percent, min_delta_ms)
    existing_regressions = [s for s in timing_signals if s.get("status") == "regressed"]

    return {
        "median_delta_percent": median_delta,
        "p95_delta_percent": p95_delta,
        "worst_delta_percent": worst_delta,
        "p95_delta_ms": p95_delta_ms,
        "effective_p95_threshold_percent": effective_p95_threshold,
        "effective_p95_threshold_ms": effective_p95_ms,
        "expected_noise": noise,
        "environment": environment,
        "classification": "environment_incompatible"
        if environment["status"] == "incompatible"
        else ("regression" if failed else "passed"),
        "regressed_frames": regressed,
        "pattern": pattern,
        # Backwards-compatible 0.2 field plus the richer 0.3 signal list.
        "modifier_regressions": existing_regressions,
        "modifier_timing_signals": timing_signals,
        "confidence": confidence,
        "failed": failed,
        "warnings": _comparison_warnings(baseline, current),
    }
