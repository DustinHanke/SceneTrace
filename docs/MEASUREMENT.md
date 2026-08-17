# Measurement model

SceneTrace 1.0 measures the wall-clock duration of moving Blender to a frame and forcing view-layer/dependency-graph evaluation. It uses `time.perf_counter_ns()` around `scene.frame_set()`, `view_layer.update()`, and `depsgraph.update()`.

This is intentionally described as **scene evaluation time**, not viewport FPS and not render time.

## Why repetitions?

Performance measurements are noisy. SceneTrace runs one warm-up by default and three measured repetitions. Repetition direction alternates, and each frame uses the median sample.

## Why P95?

Averages can hide bad animation spikes. P95 and worst-frame values make regressions in a small range of frames visible.

## Why two regression thresholds?

A tiny measurement moving from 0.2 ms to 0.4 ms is a 100% increase but rarely meaningful. A frame is marked regressed only when it exceeds both:

- percent threshold (default 20%)
- absolute delta threshold (default 2 ms)

## Environment validity

Headless benchmarks run with `--factory-startup --background`. Blender major/minor version, startup mode, measurement mode, and benchmark-settings differences invalidate a comparison instead of producing a regression. OS and architecture differences are reported as warnings. The artifact records these values for auditability.

## Attribution language

SceneTrace does not say that a changed modifier **caused** a slowdown. It reports scene changes since the baseline next to regressed frames as **correlation clues**, and reports a likely contributor only when Blender timing evidence supports it.
