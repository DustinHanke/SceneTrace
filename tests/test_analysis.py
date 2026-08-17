import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scenetrace_analysis", ROOT / "blender" / "scenetrace" / "analysis.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def make_run(values, modifier_summary=None):
    samples = [{"frame": frame, "ms": ms} for frame, ms in values]
    return {
        "version": 2,
        "measurement_mode": "depsgraph_frame_update_wall_time",
        "settings": {"frame_start": values[0][0], "frame_end": values[-1][0], "frame_step": 1},
        "samples": samples,
        "summary": analysis.summarize(samples),
        "modifier_summary": modifier_summary or [],
        "scene_snapshot": {},
    }


def test_percentile_and_summary():
    run = make_run([(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)])
    assert run["summary"]["median_ms"] == 30
    assert run["summary"]["worst_frame"] == 5
    assert run["summary"]["p95_ms"] == 48


def test_detects_regressed_frame():
    base = make_run([(1, 10), (2, 10), (3, 10), (4, 10), (5, 10)])
    cur = make_run([(1, 10), (2, 10), (3, 35), (4, 10), (5, 10)])
    result = analysis.compare_runs(base, cur, threshold_percent=20, min_delta_ms=2)
    assert result["failed"]
    assert result["regressed_frames"][0]["frame"] == 3
    assert result["pattern"]["kind"] == "localized"


def test_ignores_tiny_noise_even_if_percent_large():
    base = make_run([(1, 0.2), (2, 0.2), (3, 0.2)])
    cur = make_run([(1, 0.4), (2, 0.2), (3, 0.2)])
    result = analysis.compare_runs(base, cur, threshold_percent=20, min_delta_ms=2)
    assert not result["regressed_frames"]


def test_aggregate_repetitions_uses_median_per_frame():
    result = analysis.aggregate_repetitions({1: [10, 100, 11], 2: [20, 21, 22]})
    assert result[0]["ms"] == 11
    assert result[1]["ms"] == 21


def test_warns_on_incompatible_frame_range():
    base = make_run([(1, 10), (2, 10)])
    cur = make_run([(1, 10), (2, 10)])
    base["settings"] = {"frame_start": 1, "frame_end": 100, "frame_step": 1}
    cur["settings"] = {"frame_start": 1, "frame_end": 50, "frame_step": 1}
    result = analysis.compare_runs(base, cur, 20, 2)
    assert any("frame end" in warning.lower() for warning in result["warnings"])


def test_calibrated_baseline_learns_run_to_run_noise():
    runs = [
        make_run([(1, 10.0), (2, 10.0), (3, 10.0)]),
        make_run([(1, 10.5), (2, 10.5), (3, 10.5)]),
        make_run([(1, 9.5), (2, 9.5), (3, 9.5)]),
        make_run([(1, 10.3), (2, 10.3), (3, 10.3)]),
        make_run([(1, 9.7), (2, 9.7), (3, 9.7)]),
    ]
    profile = analysis.build_baseline_profile(runs)
    assert profile["calibration_runs"] == 5
    assert profile["noise"]["quality"] in {"EXCELLENT", "GOOD"}
    assert profile["noise"]["p95"]["percent"] > 0


def test_noise_floor_can_raise_effective_threshold():
    # Deliberately noisy calibration: ~10% normal variation. A 12% current
    # change should not be treated as meaningful when the learned floor is doubled.
    runs = [
        make_run([(1, 10.0), (2, 10.0), (3, 10.0)]),
        make_run([(1, 11.0), (2, 11.0), (3, 11.0)]),
        make_run([(1, 9.0), (2, 9.0), (3, 9.0)]),
        make_run([(1, 10.8), (2, 10.8), (3, 10.8)]),
        make_run([(1, 9.2), (2, 9.2), (3, 9.2)]),
    ]
    profile = analysis.build_baseline_profile(runs)
    current = make_run([(1, 11.2), (2, 11.2), (3, 11.2)])
    result = analysis.compare_runs(profile, current, threshold_percent=5, min_delta_ms=0.1)
    assert result["effective_p95_threshold_percent"] > 5
    assert not result["failed"]


def test_persistent_regression_is_summarized():
    base = make_run([(i, 10) for i in range(1, 11)])
    cur = make_run([(i, 30) for i in range(1, 11)])
    result = analysis.compare_runs(base, cur, threshold_percent=20, min_delta_ms=2)
    assert result["pattern"]["kind"] == "persistent"
    assert result["pattern"]["affected_frames"] == 10
    assert result["pattern"]["ranges"] == [{"start": 1, "end": 10}]


def test_modifier_timing_regression_is_reported():
    mod_base = [{"object": "Head", "modifier": "Armature", "type": "ARMATURE", "p95_ms": 2.0}]
    mod_cur = [{"object": "Head", "modifier": "Armature", "type": "ARMATURE", "p95_ms": 8.0}]
    base = make_run([(1, 10), (2, 10), (3, 10)], mod_base)
    cur = make_run([(1, 30), (2, 30), (3, 30)], mod_cur)
    result = analysis.compare_runs(base, cur, threshold_percent=20, min_delta_ms=2)
    assert result["modifier_regressions"][0]["object"] == "Head"
    assert result["modifier_regressions"][0]["delta_percent"] == 300.0


def test_new_modifier_timing_is_reported_without_baseline_value():
    base = make_run([(1, 10), (2, 10), (3, 10)], [])
    cur = make_run(
        [(1, 30), (2, 30), (3, 30)],
        [{"object": "Head", "modifier": "Subdivision", "type": "SUBSURF", "p95_ms": 12.0}],
    )
    result = analysis.compare_runs(base, cur, threshold_percent=20, min_delta_ms=2)
    signal = result["modifier_timing_signals"][0]
    assert signal["status"] == "new"
    assert signal["baseline_p95_ms"] is None
    assert signal["current_p95_ms"] == 12.0


def test_diagnosis_groups_and_ranks_timing_plus_scene_evidence():
    base = make_run([(1, 10), (2, 10), (3, 10)], [])
    cur = make_run(
        [(1, 30), (2, 30), (3, 30)],
        [{"object": "Head", "modifier": "Subdivision", "type": "SUBSURF", "p95_ms": 12.0}],
    )
    signals = analysis.compare_modifier_timings(base, cur, 20, 2)
    changes = [
        {
            "entity": "Head",
            "kind": "modifier_added",
            "label": "Modifier added: Subdivision (SUBSURF)",
            "before": None,
            "after": "Subdivision",
            "priority": 85,
        },
        {
            "entity": "Head",
            "kind": "evaluated_geometry",
            "label": "Evaluated Vertices",
            "before": 1000,
            "after": 16000,
            "priority": 100,
            "delta": 15000,
            "delta_percent": 1500.0,
        },
    ]
    diagnosis = analysis.build_diagnosis(base, cur, changes, signals)
    top = diagnosis["contributors"][0]
    assert top["object"] == "Head"
    assert top["confidence"] == "HIGH"
    assert "Subdivision" in top["headline"]
    assert diagnosis["coverage"]["largest_measured_modifier_delta_ms"] == 12.0


def test_attribution_coverage_does_not_sum_parallel_modifier_timings():
    base = make_run([(1, 10), (2, 10), (3, 10)], [])
    cur = make_run([(1, 30), (2, 30), (3, 30)], [])
    signals = [
        {"status": "new", "object": "A", "modifier": "One", "type": "NODES", "current_p95_ms": 8.0, "delta_ms": 8.0},
        {"status": "new", "object": "B", "modifier": "Two", "type": "NODES", "current_p95_ms": 7.0, "delta_ms": 7.0},
    ]
    diagnosis = analysis.build_diagnosis(base, cur, [], signals)
    # Frame P95 delta is 20 ms. Conservative coverage must use 8 ms, not 8+7.
    assert diagnosis["coverage"]["conservative_covered_ms"] == 8.0
    assert diagnosis["coverage"]["remaining_ms"] == 12.0


def test_frame_budget_is_explicitly_evaluation_only():
    budget = analysis.frame_budget({"p95_ms": 20.0}, 60)
    assert budget["target_fps"] == 60
    assert round(budget["budget_ms"], 2) == 16.67
    assert not budget["passes"]
    assert budget["remaining_ms"] < 0
    assert "evaluation" in budget["note"].lower()


def test_timeline_downsampling_preserves_current_spike():
    base = make_run([(i, 10.0) for i in range(1, 101)])
    current_values = [(i, 10.0) for i in range(1, 101)]
    current_values[56] = (57, 80.0)
    cur = make_run(current_values)
    points = analysis.build_timeline(base, cur, max_points=10)
    assert len(points) <= 10
    assert any(point["frame"] == 57 and point["current_ms"] == 80.0 for point in points)


def test_diagnosis_exposes_compact_product_summary():
    base = make_run([(1, 1.5), (2, 1.5), (3, 1.5)], [])
    cur = make_run(
        [(1, 75.0), (2, 75.0), (3, 75.0)],
        [{"object": "TestCharacterMesh", "modifier": "Subdivision", "type": "SUBSURF", "p95_ms": 70.0}],
    )
    signals = analysis.compare_modifier_timings(base, cur, 20, 2)
    changes = [{
        "entity": "TestCharacterMesh",
        "kind": "evaluated_geometry",
        "label": "Evaluated Vertices",
        "before": 65000,
        "after": 260000,
        "priority": 100,
        "delta": 195000,
        "delta_percent": 300.0,
    }]
    diagnosis = analysis.build_diagnosis(base, cur, changes, signals)
    summary = diagnosis["summary"]
    assert summary["object"] == "TestCharacterMesh"
    assert summary["modifier"] == "Subdivision"
    assert summary["geometry_before"] == 65000
    assert summary["geometry_after"] == 260000
    assert summary["coverage_percent"] > 90


def test_diagnosis_omits_transform_only_low_priority_noise():
    base = make_run([(1, 10), (2, 10), (3, 10)], [])
    cur = make_run([(1, 30), (2, 30), (3, 30)], [])
    changes = [{
        "entity": "TestMesh",
        "kind": "object_property",
        "label": "Location",
        "before": [1.0, 2.0, 3.0],
        "after": [1.001, 2.0, 3.0],
        "priority": 35,
    }]
    diagnosis = analysis.build_diagnosis(base, cur, changes, [])
    assert diagnosis["contributors"] == []


def test_graph_series_contains_budget_and_endpoints():
    base = make_run([(i, 10.0) for i in range(1, 21)])
    cur = make_run([(i, 12.0) for i in range(1, 21)])
    graph = analysis.build_graph_series(base, cur, target_fps=50, max_points=5)
    assert graph["budget_ms"] == 20.0
    assert graph["frame_start"] == 1
    assert graph["frame_end"] == 20
    assert graph["points"][0]["frame"] == 1
    assert graph["points"][-1]["frame"] == 20
    assert graph["y_max_ms"] > 20.0


def test_graph_series_preserves_spike_when_downsampled():
    base = make_run([(i, 2.0) for i in range(1, 101)])
    values = [(i, 2.0) for i in range(1, 101)]
    values[56] = (57, 80.0)
    cur = make_run(values)
    graph = analysis.build_graph_series(base, cur, target_fps=30, max_points=10)
    assert any(p["frame"] == 57 and p["current_ms"] == 80.0 for p in graph["points"])


def test_product_status_stable_uses_learned_noise_context():
    runs = [
        make_run([(1, 10.0), (2, 10.0), (3, 10.0)]),
        make_run([(1, 10.5), (2, 10.5), (3, 10.5)]),
        make_run([(1, 9.5), (2, 9.5), (3, 9.5)]),
        make_run([(1, 10.2), (2, 10.2), (3, 10.2)]),
    ]
    baseline = analysis.build_baseline_profile(runs)
    cur = make_run([(1, 9.8), (2, 9.8), (3, 9.8)])
    comparison = analysis.compare_runs(baseline, cur, threshold_percent=20, min_delta_ms=2)
    comparison["diagnosis"] = analysis.build_diagnosis(baseline, cur, [], comparison["modifier_timing_signals"])
    status = analysis.build_product_status(baseline, cur, comparison, 30)
    assert status["state"] == "stable"
    assert status["title"] == "PERFORMANCE STABLE"
    assert status["noise_percent"] > 0
    assert "variance" in status["pattern_text"].lower()


def test_product_status_regression_promotes_likely_source():
    base = make_run([(1, 1.5), (2, 1.5), (3, 1.5)], [])
    cur = make_run(
        [(1, 75.0), (2, 75.0), (3, 75.0)],
        [{"object": "TestCharacterMesh", "modifier": "Subdivision", "type": "SUBSURF", "p95_ms": 70.0}],
    )
    comparison = analysis.compare_runs(base, cur, threshold_percent=20, min_delta_ms=2)
    changes = [{
        "entity": "TestCharacterMesh",
        "kind": "evaluated_geometry",
        "label": "Evaluated Triangles",
        "before": 131796,
        "after": 527184,
        "priority": 100,
        "delta": 395388,
        "delta_percent": 300.0,
    }]
    comparison["diagnosis"] = analysis.build_diagnosis(base, cur, changes, comparison["modifier_timing_signals"])
    status = analysis.build_product_status(base, cur, comparison, 30)
    assert status["state"] == "regression"
    assert status["primary"] == "Subdivision on TestCharacterMesh"
    assert status["diagnosis"]["coverage_percent"] > 90


def test_graph_layout_respects_left_and_right_ui_insets():
    layout = analysis.compute_graph_layout(1200, 800, left_inset=52, right_inset=320, position="TOP")
    assert layout is not None
    assert layout["x"] >= 68  # toolbar + breathing room
    assert layout["x"] + layout["width"] <= 1200 - 320 - 16


def test_graph_layout_reflows_when_sidebar_opens():
    wide = analysis.compute_graph_layout(1200, 800, left_inset=52, right_inset=0, position="TOP")
    narrow = analysis.compute_graph_layout(1200, 800, left_inset=52, right_inset=320, position="TOP")
    assert wide is not None and narrow is not None
    assert narrow["x"] + narrow["width"] <= 864
    assert (narrow["x"], narrow["width"]) != (wide["x"], wide["width"])


def test_graph_layout_supports_bottom_position():
    top = analysis.compute_graph_layout(1000, 700, 0, 0, "TOP")
    bottom = analysis.compute_graph_layout(1000, 700, 0, 0, "BOTTOM")
    assert top is not None and bottom is not None
    assert top["y"] > bottom["y"]
    assert bottom["y"] == 28


def test_graph_layout_hides_when_viewport_is_too_narrow():
    assert analysis.compute_graph_layout(250, 500, 0, 0, "TOP") is None


def test_comparison_warns_when_execution_mode_differs():
    base = make_run([(1, 10.0), (2, 10.0)])
    cur = make_run([(1, 10.0), (2, 10.0)])
    base["settings"] = {"execution_mode": "interactive"}
    cur["settings"] = {"execution_mode": "background"}
    warnings = analysis._comparison_warnings(base, cur)
    assert any("Execution mode differs" in warning for warning in warnings)


def test_environment_compatibility_rejects_different_blender_measurement_versions():
    baseline = {
        "environment": {
            "blender_version": [4, 4, 0],
            "startup_mode": "factory",
            "measurement_mode": "depsgraph_frame_update_wall_time",
            "benchmark_settings": {"repetitions": 3},
        }
    }
    current = {
        "environment": {
            "blender_version": [4, 5, 0],
            "startup_mode": "factory",
            "measurement_mode": "depsgraph_frame_update_wall_time",
            "benchmark_settings": {"repetitions": 3},
        }
    }

    result = analysis.environment_compatibility(baseline, current)

    assert result["status"] == "incompatible"
    assert any("Blender" in reason for reason in result["reasons"])
