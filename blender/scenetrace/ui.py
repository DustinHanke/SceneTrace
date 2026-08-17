from __future__ import annotations

import json
import textwrap

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel

from .analysis import build_baseline_profile, build_diagnosis, build_product_status, build_timeline, compare_runs, frame_budget
from .benchmark import run_benchmark
from .snapshot import diff_snapshots
from .storage import load_baseline, save_baseline, save_report
from .graph_overlay import ensure_graph_handler, remove_graph_handler, tag_redraw


def _last_run(scene) -> dict | None:
    try:
        return json.loads(scene.scenetrace_last_run_json) if scene.scenetrace_last_run_json else None
    except Exception:
        return None


def _comparison(scene) -> dict | None:
    try:
        return json.loads(scene.scenetrace_comparison_json) if scene.scenetrace_comparison_json else None
    except Exception:
        return None


def _changes(scene) -> list[dict]:
    try:
        return json.loads(scene.scenetrace_changes_json) if scene.scenetrace_changes_json else []
    except Exception:
        return []


def _baseline_run(baseline: dict) -> dict:
    return baseline.get("aggregate", baseline)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:.3g}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, list):
        if len(value) <= 4 and len(str(value)) <= 45:
            return str(value)
        return f"{len(value)} items"
    text = str(value)
    return text if len(text) <= 48 else text[:45] + "..."


def _wrapped(layout, text: str, width: int = 48):
    for line in textwrap.wrap(text, width=width) or [""]:
        layout.label(text=line)


def _run_from_scene(context) -> dict:
    scene = context.scene
    return run_benchmark(
        context,
        scene.scenetrace_frame_start,
        scene.scenetrace_frame_end,
        scene.scenetrace_frame_step,
        scene.scenetrace_repetitions,
        scene.scenetrace_warmups,
        scene.scenetrace_capture_modifier_timings,
    )


def _apply_comparison(scene, run: dict):
    baseline = load_baseline()
    if not baseline:
        scene.scenetrace_comparison_json = ""
        scene.scenetrace_changes_json = ""
        return None

    comparison = compare_runs(
        baseline,
        run,
        scene.scenetrace_threshold_percent,
        scene.scenetrace_min_delta_ms,
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
    scene.scenetrace_comparison_json = json.dumps(comparison)
    scene.scenetrace_changes_json = json.dumps(changes)
    return comparison


class SCENETRACE_OT_benchmark(Operator):
    bl_idname = "scenetrace.benchmark"
    bl_label = "Run Benchmark"
    bl_description = "Measure scene evaluation wall time across the configured frame range"

    def execute(self, context):
        scene = context.scene
        if scene.scenetrace_frame_end < scene.scenetrace_frame_start:
            self.report({"ERROR"}, "End frame must be greater than or equal to start frame")
            return {"CANCELLED"}
        try:
            run = _run_from_scene(context)
        except Exception as exc:
            self.report({"ERROR"}, f"Benchmark failed: {exc}")
            return {"CANCELLED"}

        scene.scenetrace_last_run_json = json.dumps(run)
        comparison = _apply_comparison(scene, run)
        if comparison:
            label = "REGRESSION" if comparison["failed"] else "STABLE"
            level = {"WARNING"} if comparison["failed"] else {"INFO"}
            self.report(level, f"SceneTrace {label}: P95 {comparison['p95_delta_percent']:+.1f}%")
        else:
            self.report({"INFO"}, f"Benchmark complete: P95 {run['summary']['p95_ms']:.2f} ms")
        return {"FINISHED"}


class SCENETRACE_OT_save_quick_baseline(Operator):
    bl_idname = "scenetrace.save_quick_baseline"
    bl_label = "Quick Baseline"
    bl_description = "Save the latest run as a one-run baseline; useful for fast tests but it cannot learn run-to-run noise"

    def execute(self, context):
        run = _last_run(context.scene)
        if not run:
            self.report({"ERROR"}, "Run a benchmark first")
            return {"CANCELLED"}
        try:
            profile = build_baseline_profile([run])
            path = save_baseline(profile)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        context.scene.scenetrace_comparison_json = ""
        context.scene.scenetrace_changes_json = ""
        self.report({"INFO"}, f"Saved quick baseline: {path}")
        return {"FINISHED"}


class SCENETRACE_OT_calibrate_baseline(Operator):
    bl_idname = "scenetrace.calibrate_baseline"
    bl_label = "Calibrate Baseline"
    bl_description = "Run several full benchmarks, learn normal variance, and save an aggregated baseline"

    def execute(self, context):
        scene = context.scene
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save the .blend file before calibrating a baseline")
            return {"CANCELLED"}
        if scene.scenetrace_frame_end < scene.scenetrace_frame_start:
            self.report({"ERROR"}, "Invalid frame range")
            return {"CANCELLED"}

        runs = []
        try:
            for _ in range(scene.scenetrace_baseline_runs):
                runs.append(_run_from_scene(context))
            profile = build_baseline_profile(runs)
            path = save_baseline(profile)
        except Exception as exc:
            self.report({"ERROR"}, f"Baseline calibration failed: {exc}")
            return {"CANCELLED"}

        latest = runs[-1]
        scene.scenetrace_last_run_json = json.dumps(latest)
        scene.scenetrace_comparison_json = ""
        scene.scenetrace_changes_json = ""
        quality = profile.get("noise", {}).get("quality", "UNKNOWN")
        p95_noise = profile.get("noise", {}).get("p95", {}).get("percent", 0.0)
        self.report({"INFO"}, f"Baseline calibrated ({quality}, P95 noise ±{p95_noise:.1f}%): {path}")
        return {"FINISHED"}


class SCENETRACE_OT_export_report(Operator):
    bl_idname = "scenetrace.export_report"
    bl_label = "Export Latest Report"
    bl_description = "Write the latest benchmark and comparison to .scenetrace/latest.json"

    def execute(self, context):
        run = _last_run(context.scene)
        if not run:
            self.report({"ERROR"}, "Run a benchmark first")
            return {"CANCELLED"}
        try:
            path = save_report(run, _comparison(context.scene), _changes(context.scene))
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Exported report: {path}")
        return {"FINISHED"}


class SCENETRACE_OT_jump_frame(Operator):
    bl_idname = "scenetrace.jump_frame"
    bl_label = "Jump"
    frame: IntProperty()

    def execute(self, context):
        context.scene.frame_set(self.frame)
        return {"FINISHED"}


class SCENETRACE_OT_focus_object(Operator):
    bl_idname = "scenetrace.focus_object"
    bl_label = "Focus Object"
    object_name: StringProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            self.report({"WARNING"}, f"Object not found: {self.object_name}")
            return {"CANCELLED"}
        for candidate in context.selected_objects:
            candidate.select_set(False)
        obj.hide_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        try:
            bpy.ops.view3d.view_selected(use_all_regions=False)
        except Exception:
            pass
        return {"FINISHED"}



class SCENETRACE_OT_toggle_graph(Operator):
    bl_idname = "scenetrace.toggle_graph"
    bl_label = "Toggle Performance Graph"
    bl_description = "Show or hide the baseline/current performance graph in the 3D Viewport"

    def execute(self, context):
        scene = context.scene
        scene.scenetrace_graph_overlay = not bool(scene.scenetrace_graph_overlay)
        if scene.scenetrace_graph_overlay:
            ensure_graph_handler()
        else:
            remove_graph_handler()
        tag_redraw()
        return {"FINISHED"}

def _draw_change(col, change: dict):
    label = change.get("label", change.get("kind", "change"))
    col.label(text=label)
    col.label(text=f"{_fmt(change.get('before'))} → {_fmt(change.get('after'))}")
    if change.get("delta_percent") is not None:
        delta = change.get("delta", 0)
        col.label(text=f"Δ {delta:+,} ({change['delta_percent']:+.1f}%)")
    if change.get("note"):
        _wrapped(col, change["note"], width=44)


def _draw_diagnosis(layout, diagnosis: dict):
    contributors = diagnosis.get("contributors", [])
    if not contributors:
        return

    section = layout.box()
    section.label(text="Likely contributors", icon="MODIFIER")
    _wrapped(section, "Ranked evidence combines measured modifier timing with geometry and modifier changes. It is not causal proof.")

    for index, contributor in enumerate(contributors[:5], start=1):
        card = section.box()
        row = card.row(align=True)
        row.label(text=f"#{index}  {contributor.get('object', '<scene>')}")
        row.label(text=f"{contributor.get('confidence', 'LOW')} · score {contributor.get('score', 0):.0f}")
        if bpy.data.objects.get(contributor.get("object", "")):
            focus = row.operator("scenetrace.focus_object", text="", icon="RESTRICT_SELECT_OFF")
            focus.object_name = contributor["object"]
        card.label(text=contributor.get("headline", "Changed object"))

        timings = contributor.get("timing_signals", [])
        if timings:
            card.label(text="Measured modifier timing")
            for signal in timings[:3]:
                if signal.get("status") == "new":
                    card.label(text=f"  {signal.get('modifier')}  NEW · {signal.get('current_p95_ms', 0.0):.2f} ms P95")
                else:
                    card.label(
                        text=(
                            f"  {signal.get('modifier')}  "
                            f"{signal.get('baseline_p95_ms', 0.0):.2f} → {signal.get('current_p95_ms', 0.0):.2f} ms "
                            f"({signal.get('delta_percent', 0.0):+.0f}%)"
                        )
                    )

        changes = contributor.get("changes", [])
        if changes:
            card.label(text="Scene evidence")
            for change in changes[:4]:
                col = card.column(align=True)
                _draw_change(col, change)

    coverage = diagnosis.get("coverage", {})
    delta = max(0.0, float(diagnosis.get("frame_p95_delta_ms", 0.0)))
    if delta > 0.0:
        coverage_box = layout.box()
        coverage_box.label(text="Attribution coverage", icon="INFO")
        coverage_box.label(text=f"Observed frame P95 delta: +{delta:.2f} ms")
        strongest = float(coverage.get("largest_measured_modifier_delta_ms", 0.0))
        if strongest > 0.0:
            coverage_box.label(
                text=(
                    f"Strongest measured modifier signal: +{strongest:.2f} ms "
                    f"({coverage.get('coverage_percent', 0.0):.0f}% of frame delta)"
                )
            )
            coverage_box.label(text=f"Not explained by that single signal: ~{coverage.get('remaining_ms', 0.0):.2f} ms")
        else:
            coverage_box.label(text="No measured modifier timing signal explains the frame regression.")
        _wrapped(coverage_box, coverage.get("note", ""), width=46)


def _bar(ratio: float, width: int = 10) -> str:
    ratio = max(0.0, min(1.0, float(ratio)))
    count = int(round(ratio * width))
    if ratio > 0 and count == 0:
        count = 1
    return "▮" * count + "·" * (width - count)


def _draw_timeline(layout, baseline: dict, run: dict):
    points = build_timeline(baseline, run, max_points=12)
    if not points:
        return
    box = layout.box()
    box.label(text="Performance timeline", icon="GRAPH")
    box.label(text="Baseline B  ·  Current C")
    for point in points:
        frame_label = (
            str(point["frame_start"])
            if point["frame_start"] == point["frame_end"]
            else f"{point['frame_start']}–{point['frame_end']}"
        )
        row = box.row(align=True)
        row.label(text=f"F {frame_label}")
        row.label(text=f"B {_bar(point['baseline_ratio'], 7)}")
        row.label(text=f"C {_bar(point['current_ratio'], 7)}")
        jump = row.operator("scenetrace.jump_frame", text=f"{point['current_ms']:.1f} ms")
        jump.frame = int(point["frame"])


def _compact_number(value) -> str:
    try:
        number = float(value)
    except Exception:
        return _fmt(value)
    absolute = abs(number)
    if absolute >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{number / 1_000:.1f}K"
    if absolute >= 100:
        return f"{number:.0f}"
    return f"{number:.1f}"


def _draw_evaluation_budget(layout, summary: dict, target_fps: int, compact: bool = False):
    budget = frame_budget(summary, target_fps)
    box = layout.box()
    icon = "CHECKMARK" if budget["passes"] else "ERROR"
    box.label(text=f"Evaluation budget · {budget['target_fps']} FPS", icon=icon)
    row = box.row(align=True)
    row.label(text=f"P95 {budget['p95_ms']:.2f} ms")
    row.label(text=f"Budget {budget['budget_ms']:.2f} ms")
    if budget["passes"]:
        box.label(text=f"{budget['utilization_percent']:.0f}% used · {max(0.0, budget['remaining_ms']):.2f} ms evaluation headroom")
    else:
        box.label(text=f"Over evaluation budget by {-budget['remaining_ms']:.2f} ms")
        if budget.get("max_theoretical_fps_from_evaluation"):
            box.label(text=f"Evaluation-only ceiling ≈ {budget['max_theoretical_fps_from_evaluation']:.1f} FPS")
    if not compact:
        _wrapped(box, "Evaluation only; viewport drawing, GPU work and display overhead are not included.", width=48)


def _draw_product_overview(layout, baseline: dict, comparison: dict, run: dict, target_fps: int):
    if not comparison:
        return
    status = build_product_status(baseline, run, comparison, target_fps)
    diagnosis = status.get("diagnosis") or {}
    failed = status["state"] == "regression"

    box = layout.box()
    box.label(text=status["title"], icon="ERROR" if failed else "CHECKMARK")
    row = box.row(align=True)
    row.label(text=f"P95 {status['baseline_p95_ms']:.2f} → {status['current_p95_ms']:.2f} ms")
    row.label(text=f"{status['delta_percent']:+.1f}%")

    if failed:
        pattern = status.get("pattern_text", "")
        row = box.row(align=True)
        row.label(text=f"Confidence {status.get('confidence', 'HIGH')}")
        if pattern:
            row.label(text=pattern)
        if diagnosis:
            box.separator()
            box.label(text="Likely source", icon="MODIFIER")
            obj = diagnosis.get("object") or "<scene>"
            modifier = diagnosis.get("modifier")
            source_line = f"{modifier} on {obj}" if modifier else diagnosis.get("headline", obj)
            box.label(text=source_line)
            evidence = box.row(align=True)
            measured = float(diagnosis.get("measured_delta_ms", 0.0))
            coverage = float(diagnosis.get("coverage_percent", 0.0))
            if measured > 0:
                evidence.label(text=f"Measured +{measured:.2f} ms")
            if coverage > 0:
                evidence.label(text=f"~{coverage:.0f}% of observed P95 delta")
            gb = diagnosis.get("geometry_before")
            ga = diagnosis.get("geometry_after")
            if gb is not None and ga is not None:
                label = (diagnosis.get("geometry_label") or "Evaluated geometry").replace("Evaluated ", "")
                pct = diagnosis.get("geometry_delta_percent")
                line = f"{label}: {_compact_number(gb)} → {_compact_number(ga)}"
                if pct is not None:
                    line += f" ({float(pct):+.0f}%)"
                box.label(text=line)
            if bpy.data.objects.get(obj):
                focus = box.operator("scenetrace.focus_object", text=f"Focus {obj}", icon="RESTRICT_SELECT_OFF")
                focus.object_name = obj
    else:
        noise = float(status.get("noise_percent", 0.0))
        box.label(text=status.get("primary", "Within expected variation"))
        if noise > 0:
            box.label(text=f"Expected P95 variation ±{noise:.1f}% · {status.get('pattern_text', '')}")
        else:
            box.label(text=status.get("pattern_text", "Within regression threshold"))
        budget = status.get("budget", {})
        if budget:
            if budget.get("passes"):
                box.label(text=f"{target_fps} FPS evaluation budget: {budget.get('utilization_percent', 0.0):.0f}% used")
            else:
                box.label(text=f"{target_fps} FPS evaluation budget exceeded by {-float(budget.get('remaining_ms', 0.0)):.2f} ms", icon="ERROR")


class SCENETRACE_PT_main(Panel):
    bl_label = "SceneTrace"
    bl_idname = "SCENETRACE_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SceneTrace"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        baseline = load_baseline()
        run = _last_run(scene)
        comparison = _comparison(scene)

        intro = layout.box()
        row = intro.row(align=True)
        row.label(text="Performance regression testing", icon="TIME")
        row.label(text="v1.0")
        if baseline:
            if baseline.get("schema") == "scenetrace-baseline":
                noise = baseline.get("noise", {})
                row = intro.row(align=True)
                row.label(text=f"Baseline {noise.get('quality', 'UNKNOWN')}", icon="CHECKMARK")
                row.label(text=f"P95 noise ±{noise.get('p95', {}).get('percent', 0.0):.1f}%")
            else:
                intro.label(text="Legacy baseline · variance unknown", icon="ERROR")
        else:
            intro.label(text="No baseline yet", icon="INFO")

        primary = layout.row(align=True)
        primary.scale_y = 1.45
        if baseline:
            primary.operator("scenetrace.benchmark", text="Re-test vs Baseline", icon="PLAY")
            primary.operator("scenetrace.calibrate_baseline", text="Recalibrate", icon="FILE_REFRESH")
        else:
            primary.operator("scenetrace.benchmark", text="Run Benchmark", icon="PLAY")
            primary.operator("scenetrace.calibrate_baseline", text="Create Baseline", icon="REC")

        if run:
            if baseline and comparison:
                _draw_product_overview(layout, baseline, comparison, run, scene.scenetrace_target_fps)

            summary = run["summary"]
            metrics = layout.box()
            metrics.label(text="Current performance", icon="GRAPH")
            row = metrics.row(align=True)
            row.label(text=f"Median {summary['median_ms']:.2f} ms")
            row.label(text=f"P95 {summary['p95_ms']:.2f} ms")
            row = metrics.row(align=True)
            row.label(text=f"Worst {summary['worst_ms']:.2f} ms")
            jump = row.operator("scenetrace.jump_frame", text=f"Frame {summary['worst_frame']}")
            jump.frame = int(summary["worst_frame"])
            metrics.prop(scene, "scenetrace_target_fps")

            if baseline and comparison:
                graph_row = layout.row(align=True)
                graph_row.scale_y = 1.15
                graph_row.operator(
                    "scenetrace.toggle_graph",
                    text="Hide Viewport Graph" if scene.scenetrace_graph_overlay else "Show Viewport Graph",
                    icon="HIDE_OFF" if scene.scenetrace_graph_overlay else "GRAPH",
                )
                if scene.scenetrace_graph_overlay:
                    graph_row.prop(scene, "scenetrace_graph_position", text="")
                graph_row.prop(scene, "scenetrace_show_timeline", text="Timeline", toggle=True)

            _draw_evaluation_budget(layout, summary, scene.scenetrace_target_fps, compact=bool(baseline and comparison))

            if baseline and comparison:
                pattern = comparison.get("pattern", {})
                if comparison.get("failed") and pattern.get("affected_frames", 0):
                    pattern_box = layout.box()
                    pattern_box.label(text="Regression pattern", icon="GRAPH")
                    row = pattern_box.row(align=True)
                    row.label(text=pattern.get("kind", "unknown").upper())
                    row.label(text=f"{pattern.get('affected_frames', 0)} / {pattern.get('total_frames', 0)} frames")
                    ranges = pattern.get("ranges", [])
                    if ranges:
                        text = ", ".join(
                            str(r["start"]) if r["start"] == r["end"] else f"{r['start']}–{r['end']}"
                            for r in ranges[:4]
                        )
                        if len(ranges) > 4:
                            text += ", …"
                        pattern_box.label(text=f"Ranges: {text}")

                if scene.scenetrace_show_timeline:
                    _draw_timeline(layout, baseline, run)

                layout.prop(scene, "scenetrace_show_details", text="Technical evidence")
                if scene.scenetrace_show_details:
                    evidence = layout.box()
                    row = evidence.row(align=True)
                    row.label(text=f"Median {comparison['median_delta_percent']:+.1f}%")
                    row.label(text=f"P95 {comparison['p95_delta_percent']:+.1f}%")
                    evidence.label(text=f"Worst {comparison['worst_delta_percent']:+.1f}%")
                    expected = comparison.get("expected_noise", {})
                    if expected:
                        evidence.label(text=f"Baseline P95 noise ±{expected.get('p95', {}).get('percent', 0.0):.1f}%")
                        evidence.label(text=f"Effective threshold +{comparison.get('effective_p95_threshold_percent', 0.0):.1f}%")
                    for warning in comparison.get("warnings", []):
                        _wrapped(evidence, f"⚠ {warning}")
                    regressed = comparison.get("regressed_frames", [])
                    if regressed:
                        evidence.label(text="Worst regressed frames")
                        for item in regressed[:5]:
                            row = evidence.row(align=True)
                            row.label(text=f"{item['frame']}: {item['baseline_ms']:.1f} → {item['current_ms']:.1f} ms ({item['delta_percent']:+.0f}%)")
                            jump = row.operator("scenetrace.jump_frame", text="", icon="PLAY")
                            jump.frame = int(item["frame"])
                    _draw_diagnosis(layout, comparison.get("diagnosis", {}))

            layout.operator("scenetrace.export_report", icon="EXPORT")

        layout.separator()
        layout.prop(scene, "scenetrace_show_setup", text="Benchmark setup")
        if scene.scenetrace_show_setup:
            settings = layout.box()
            row = settings.row(align=True)
            row.prop(scene, "scenetrace_frame_start")
            row.prop(scene, "scenetrace_frame_end")
            settings.prop(scene, "scenetrace_frame_step")
            row = settings.row(align=True)
            row.prop(scene, "scenetrace_repetitions")
            row.prop(scene, "scenetrace_warmups")
            settings.prop(scene, "scenetrace_capture_modifier_timings")
            settings.prop(scene, "scenetrace_threshold_percent")
            settings.prop(scene, "scenetrace_min_delta_ms")
            settings.prop(scene, "scenetrace_baseline_runs")
            row = settings.row(align=True)
            row.operator("scenetrace.save_quick_baseline", text="Quick Baseline", icon="BOOKMARKS")
            row.operator("scenetrace.calibrate_baseline", text="Calibrate Baseline", icon="FILE_REFRESH")


def _graph_setting_updated(_self, _context):
    tag_redraw()


def register_properties():
    bpy.types.Scene.scenetrace_frame_start = IntProperty(name="Start", default=1)
    bpy.types.Scene.scenetrace_frame_end = IntProperty(name="End", default=100)
    bpy.types.Scene.scenetrace_frame_step = IntProperty(name="Step", default=1, min=1, max=1000)
    bpy.types.Scene.scenetrace_repetitions = IntProperty(name="Repeats", default=3, min=1, max=20)
    bpy.types.Scene.scenetrace_warmups = IntProperty(name="Warmups", default=1, min=0, max=10)
    bpy.types.Scene.scenetrace_baseline_runs = IntProperty(name="Calibration runs", default=5, min=3, max=10)
    bpy.types.Scene.scenetrace_capture_modifier_timings = BoolProperty(name="Capture modifier timings", default=True)
    bpy.types.Scene.scenetrace_threshold_percent = FloatProperty(name="Regression threshold %", default=20.0, min=0.0, max=1000.0)
    bpy.types.Scene.scenetrace_min_delta_ms = FloatProperty(name="Min frame delta ms", default=2.0, min=0.0, max=1000.0)
    bpy.types.Scene.scenetrace_target_fps = IntProperty(
        name="Target FPS", default=30, min=1, max=240, update=_graph_setting_updated
    )
    bpy.types.Scene.scenetrace_show_timeline = BoolProperty(name="Compact timeline", default=False)
    bpy.types.Scene.scenetrace_graph_overlay = BoolProperty(name="Viewport performance graph", default=False, options={"SKIP_SAVE"})
    bpy.types.Scene.scenetrace_graph_position = EnumProperty(
        name="Graph Position",
        description="Place the viewport performance graph above or below the scene",
        items=[("TOP", "Top", "Place graph at the top of the usable viewport"), ("BOTTOM", "Bottom", "Place graph at the bottom of the usable viewport")],
        default="TOP",
        update=_graph_setting_updated,
    )
    bpy.types.Scene.scenetrace_show_details = BoolProperty(name="Show technical evidence", default=False)
    bpy.types.Scene.scenetrace_show_setup = BoolProperty(name="Benchmark setup", default=False)
    bpy.types.Scene.scenetrace_last_run_json = StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.scenetrace_comparison_json = StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.scenetrace_changes_json = StringProperty(options={"HIDDEN", "SKIP_SAVE"})


def unregister_properties():
    for name in [
        "scenetrace_frame_start", "scenetrace_frame_end", "scenetrace_frame_step",
        "scenetrace_repetitions", "scenetrace_warmups", "scenetrace_baseline_runs",
        "scenetrace_capture_modifier_timings", "scenetrace_threshold_percent",
        "scenetrace_min_delta_ms", "scenetrace_target_fps", "scenetrace_show_timeline", "scenetrace_graph_overlay", "scenetrace_graph_position",
        "scenetrace_show_details", "scenetrace_show_setup", "scenetrace_last_run_json",
        "scenetrace_comparison_json", "scenetrace_changes_json",
    ]:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)


CLASSES = (
    SCENETRACE_OT_benchmark,
    SCENETRACE_OT_save_quick_baseline,
    SCENETRACE_OT_calibrate_baseline,
    SCENETRACE_OT_export_report,
    SCENETRACE_OT_jump_frame,
    SCENETRACE_OT_focus_object,
    SCENETRACE_OT_toggle_graph,
    SCENETRACE_PT_main,
)
