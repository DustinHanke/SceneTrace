# SceneTrace roadmap

## Product thesis

SceneTrace is **performance regression testing for Blender scenes and pipelines**. It is not a generic optimizer or static complexity dashboard.

The product has now validated the core loop on real Blender scenes: learn baseline variance, detect a regression, identify the affected frame range, correlate structural changes, and use Blender-reported modifier timing as measured attribution evidence where available.

## Delivered

### v0.1–0.3 — measurement and attribution

- Frame/dependency-graph wall-clock evaluation benchmark.
- Repeated samples, warmups, Median/P95/Worst.
- Baselines and regression thresholds.
- Noise-aware calibrated baselines.
- Persistent/widespread/localized regression patterns.
- Scene snapshots and semantic modifier/geometry diffs.
- Blender `Modifier.execution_time` evidence.
- Ranked likely contributors and conservative attribution coverage.

### v0.4–0.7 — Blender productization + standalone comparison

- Diagnosis-first Blender panel.
- Evaluation budget/FPS context.
- Region-aware viewport performance graph.
- Animation/driver-aware structural filtering.
- Rust CLI understands SceneTrace folders directly.
- CI-style PASS/FAIL/ERROR exit codes.

### v0.8 — headless single-scene benchmarking

- `scenetrace baseline file.blend`.
- `scenetrace test file.blend`.
- Automatic Blender discovery.
- Blender `--background` orchestration and timeout handling.
- Separate headless vs interactive baselines.
- Headless diagnosis parity with the Blender add-on.

### Project runner

- `scenetrace baseline .` and `scenetrace test .`.
- Recursive project discovery with include/exclude globs.
- Isolated per-asset headless baselines.
- Bounded parallel Blender workers.
- Per-asset timeout/crash isolation.
- BLAKE3 `.blend` hashing and changed-only cache.
- Aggregate project PASS/FAIL/ERROR report.
- JSON and Markdown reports.
- GitHub Actions step-summary output.

## v1.0 target (completed)

v1.0 delivered release quality rather than new measurement primitives:

- compile/test hardening across Windows, Linux, and supported Blender versions;
- dependency-aware cache keys rather than `.blend` bytes only;
- packaged Rust CLI binaries;
- a first-party GitHub Actions workflow/template;
- stable report/baseline schemas and migration rules;
- deterministic demo generation and real-Blender CI smoke coverage;
- documentation and a reproducible demo project;
- clear hardware/environment fingerprinting and comparison warnings;
- polished installation/distribution for both extension and CLI.

Implemented 1.0 reliability work also includes iterative non-recursive project dispatch, metadata-directory and symlink/junction-safe discovery, atomic unique-temp persistence with Windows lock retries, versioned artifact contracts, environment incompatibility classification, and dependency-aware `--changed` fingerprints.

## Later possibilities

- Git changed-file integration that maps dependency changes back to affected `.blend` scenes;
- additional DCC benchmark probes behind the same Rust runner;
- historical dashboards;
- distributed workers;
- named benchmark scenarios/platform profiles.

These are intentionally deferred after 1.0 while the Blender workflow matures. Signing/notarization remains a release-credential dependency.
