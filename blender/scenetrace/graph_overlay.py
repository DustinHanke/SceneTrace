# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .analysis import build_graph_series, compute_graph_layout
from .storage import baseline_path, load_baseline


_HANDLER = None
_BASELINE_CACHE = {"path": None, "mtime_ns": None, "data": None}


def _rgba(hex_value: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    value = hex_value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)) + (alpha,)


def _font_size(font_id: int, size: int):
    try:
        blf.size(font_id, size)
    except TypeError:
        blf.size(font_id, size, 72)


def _text(x: float, y: float, text: str, size: int = 12, color=(1, 1, 1, 1)):
    font_id = 0
    _font_size(font_id, size)
    try:
        blf.color(font_id, *color)
    except Exception:
        pass
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, str(text))


def _draw_segments(shader, segments, color, width=1.0):
    if not segments:
        return
    coords = []
    for a, b in segments:
        coords.extend((a, b))
    batch = batch_for_shader(shader, "LINES", {"pos": coords})
    gpu.state.line_width_set(width)
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_polyline(shader, points, color, width=2.0):
    if len(points) < 2:
        return
    _draw_segments(shader, list(zip(points[:-1], points[1:])), color, width)


def _draw_rect(shader, x: float, y: float, width: float, height: float, color):
    vertices = [
        (x, y), (x + width, y), (x + width, y + height),
        (x, y), (x + width, y + height), (x, y + height),
    ]
    batch = batch_for_shader(shader, "TRIS", {"pos": vertices})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _cached_baseline() -> dict | None:
    """Avoid reading baseline.json on every viewport redraw."""
    path = baseline_path()
    if path is None or not path.is_file():
        _BASELINE_CACHE.update({"path": None, "mtime_ns": None, "data": None})
        return None
    try:
        mtime_ns = path.stat().st_mtime_ns
    except Exception:
        return load_baseline()
    if _BASELINE_CACHE.get("path") == str(path) and _BASELINE_CACHE.get("mtime_ns") == mtime_ns:
        return _BASELINE_CACHE.get("data")
    data = load_baseline()
    _BASELINE_CACHE.update({"path": str(path), "mtime_ns": mtime_ns, "data": data})
    return data


def _last_run(scene) -> dict | None:
    try:
        raw = getattr(scene, "scenetrace_last_run_json", "")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _comparison(scene) -> dict | None:
    try:
        raw = getattr(scene, "scenetrace_comparison_json", "")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _overlapping_ui_insets(area, window_region) -> tuple[int, int]:
    """Return T-toolbar/N-sidebar overlap measured in WINDOW coordinates.

    Blender exposes an Area as multiple Regions. The SceneTrace draw handler is
    attached to the WINDOW region, while TOOLS and UI regions may occupy/overlay
    space on its left and right edges. Calculating their rectangle intersection
    each redraw makes the graph reflow when T/N panels are toggled or resized.
    """
    left = 0
    right = 0
    window_left = int(getattr(window_region, "x", 0))
    window_right = window_left + int(getattr(window_region, "width", 0))

    for other in getattr(area, "regions", ()):
        if other == window_region or int(getattr(other, "width", 0)) <= 1:
            continue
        region_type = getattr(other, "type", "")
        if region_type not in {"TOOLS", "UI"}:
            continue
        other_left = int(getattr(other, "x", 0))
        other_right = other_left + int(getattr(other, "width", 0))
        overlap_left = max(window_left, other_left)
        overlap_right = min(window_right, other_right)
        if overlap_right <= overlap_left:
            continue
        if region_type == "TOOLS":
            left = max(left, overlap_right - window_left)
        else:  # UI / N-panel
            right = max(right, window_right - overlap_left)
    return left, right


def _baseline_run(baseline: dict) -> dict:
    return baseline.get("aggregate", baseline)


def _draw_graph():
    context = bpy.context
    scene = getattr(context, "scene", None)
    region = getattr(context, "region", None)
    area = getattr(context, "area", None)
    if not scene or not region or not area or area.type != "VIEW_3D":
        return
    if not getattr(scene, "scenetrace_graph_overlay", False):
        return

    run = _last_run(scene)
    baseline = _cached_baseline()
    if not run or not baseline:
        return

    graph = build_graph_series(
        baseline,
        run,
        getattr(scene, "scenetrace_target_fps", 30),
        max_points=100,
    )
    points = graph.get("points", [])
    if len(points) < 2:
        return

    comparison = _comparison(scene) or {}
    failed = bool(comparison.get("failed"))
    left_inset, right_inset = _overlapping_ui_insets(area, region)
    layout = compute_graph_layout(
        region.width,
        region.height,
        left_inset,
        right_inset,
        getattr(scene, "scenetrace_graph_position", "TOP"),
    )
    if not layout:
        return

    x0 = layout["x"]
    y0 = layout["y"]
    width = layout["width"]
    height = layout["height"]
    compact = bool(layout.get("compact_labels"))

    left_axis = 54 if not compact else 46
    right_pad = 18
    top_pad = 54 if not compact else 48
    bottom_pad = 34
    plot_x = x0 + left_axis
    plot_y = y0 + bottom_pad
    plot_w = width - left_axis - right_pad
    plot_h = height - top_pad - bottom_pad

    y_max = max(1e-6, float(graph.get("y_max_ms", 1.0)))
    f0 = int(graph.get("frame_start", points[0]["frame"]))
    f1 = int(graph.get("frame_end", points[-1]["frame"]))
    frame_span = max(1, f1 - f0)

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    try:
        _draw_rect(shader, x0, y0, width, height, _rgba("111317", 0.92))

        # Grid.
        grid_segments = []
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            yy = plot_y + plot_h * fraction
            grid_segments.append(((plot_x, yy), (plot_x + plot_w, yy)))
        _draw_segments(shader, grid_segments, _rgba("5d626c", 0.42), 1.0)

        def pos(frame: int, ms: float):
            x = plot_x + ((frame - f0) / frame_span) * plot_w
            y = plot_y + min(1.0, max(0.0, ms / y_max)) * plot_h
            return (x, y)

        baseline_points = [pos(int(p["frame"]), float(p["baseline_ms"])) for p in points]
        current_points = [pos(int(p["frame"]), float(p["current_ms"])) for p in points]
        current_color = _rgba("ef6461" if failed else "58c79c", 1.0)
        _draw_polyline(shader, baseline_points, _rgba("a7adb8", 0.95), 2.0)
        _draw_polyline(shader, current_points, current_color, 2.5)

        budget_ms = float(graph.get("budget_ms", 0.0))
        if 0.0 < budget_ms <= y_max:
            budget_y = plot_y + (budget_ms / y_max) * plot_h
            _draw_segments(
                shader,
                [((plot_x, budget_y), (plot_x + plot_w, budget_y))],
                _rgba("6ea8fe", 0.9),
                1.5,
            )
            if not compact:
                _text(
                    plot_x + plot_w - 118,
                    budget_y + 5,
                    f"{graph.get('target_fps', 30)} FPS budget",
                    11,
                    _rgba("9fc5ff"),
                )

        # Worst-current-frame marker. Downsampling preserves the worst point in
        # each bucket, so the actual worst frame should normally be present.
        worst_frame = int(run.get("summary", {}).get("worst_frame", 0))
        worst_ms = float(run.get("summary", {}).get("worst_ms", 0.0))
        if f0 <= worst_frame <= f1 and worst_ms > 0:
            wx, wy = pos(worst_frame, worst_ms)
            marker = 5.0
            _draw_segments(
                shader,
                [((wx - marker, wy), (wx + marker, wy)), ((wx, wy - marker), (wx, wy + marker))],
                _rgba("ffffff", 0.95),
                1.5,
            )
            label_x = min(plot_x + plot_w - 115, max(plot_x + 6, wx + 7))
            label_y = min(plot_y + plot_h - 14, wy + 8)
            _text(label_x, label_y, f"Worst f{worst_frame} · {worst_ms:.1f} ms", 10, _rgba("ffffff"))

        base_summary = _baseline_run(baseline).get("summary", {})
        base_p95 = float(base_summary.get("p95_ms", 0.0))
        cur_p95 = float(run.get("summary", {}).get("p95_ms", 0.0))
        title = "SceneTrace · regression" if failed else "SceneTrace · stable"
        _text(x0 + 14, y0 + height - 22, title, 14, _rgba("ffffff"))
        _text(x0 + 14, y0 + height - 40, f"P95 {base_p95:.2f} → {cur_p95:.2f} ms", 10, _rgba("c9cdd4"))

        if budget_ms > 0:
            ratio = cur_p95 / budget_ms
            budget_status = (
                f"{ratio:.1f}× over budget" if ratio > 1.0 else f"{ratio * 100:.0f}% of budget"
            )
            _text(x0 + width - (108 if compact else 145), y0 + height - 40, budget_status, 10, _rgba("9fc5ff"))

        _text(plot_x, y0 + 12, f"Frame {f0}", 10, _rgba("afb4be"))
        _text(plot_x + plot_w - 62, y0 + 12, f"{f1}", 10, _rgba("afb4be"))
        _text(x0 + 7, plot_y + plot_h - 4, f"{y_max:.1f}", 10, _rgba("afb4be"))
        _text(x0 + 15, plot_y - 3, "0 ms", 10, _rgba("afb4be"))

        if not compact:
            legend_y = y0 + height - 22
            _text(x0 + width - 245, legend_y, "Baseline", 11, _rgba("a7adb8"))
            _text(x0 + width - 165, legend_y, "Current", 11, current_color)
            _text(x0 + width - 82, legend_y, "Budget", 11, _rgba("9fc5ff"))
    finally:
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set("NONE")


def ensure_graph_handler():
    global _HANDLER
    if _HANDLER is None:
        _HANDLER = bpy.types.SpaceView3D.draw_handler_add(_draw_graph, (), "WINDOW", "POST_PIXEL")
    tag_redraw()


def remove_graph_handler():
    global _HANDLER
    if _HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_HANDLER, "WINDOW")
        except Exception:
            pass
        _HANDLER = None
    _BASELINE_CACHE.update({"path": None, "mtime_ns": None, "data": None})
    tag_redraw()


def tag_redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass
