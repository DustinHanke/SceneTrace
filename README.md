# SceneTrace 1.0

**Performance regression testing for Blender scenes and projects.**

SceneTrace measures dependency-graph frame evaluation, learns normal run-to-run variance, detects meaningful regressions, and correlates them with scene changes and Blender-reported modifier timings.

## Project-wide performance testing

The Rust CLI runs single scenes and complete projects.

For one scene, the single-file workflow remains unchanged:

```powershell
scenetrace baseline "C:\SceneTraceExamples\character.blend"
scenetrace test "C:\SceneTraceExamples\character.blend"
```

For a project containing multiple `.blend` files:

```powershell
scenetrace baseline .
scenetrace test .
```

The Rust CLI now handles:

- recursive `.blend` discovery;
- include/exclude globs from `.scenetrace/config.json`;
- a separate calibrated headless baseline for every asset;
- bounded parallel Blender workers;
- per-asset timeouts and crash isolation;
- BLAKE3 file hashing and a project cache;
- `--changed` to skip unchanged assets after a passing test;
- aggregate PASS / FAIL / ERROR reporting;
- JSON project reports;
- Markdown reports and GitHub Actions step summaries;
- CI exit codes (`0` pass, `1` regression, `2` execution/input error).

### Project storage

Project mode keeps each scene isolated so two `.blend` files in the same directory never overwrite one another's benchmark data.

```text
project/
├── characters/
│   ├── hero.blend
│   └── enemy.blend
├── environments/
│   └── forest.blend
└── .scenetrace/
    ├── config.json
    ├── cache.json
    └── assets/
        ├── hero-7c4a1e53d2/
        │   ├── headless-baseline.json
        │   ├── headless-latest.json
        │   └── headless.log
        ├── enemy-4eac7d0291/
        │   └── ...
        └── forest-bd91f12ca2/
            └── ...
```

The asset directory suffix is derived from the project-relative path, so duplicate file names in different folders are safe.

## Blender extension

Install `scenetrace-blender-1.0.0.zip` using Blender 4.2+:

1. Edit → Preferences → Get Extensions.
2. Open the menu and choose **Install from Disk**.
3. Select the SceneTrace ZIP.
4. Open a `.blend`, press `N` in the 3D Viewport, and choose the **SceneTrace** tab.

The interactive workflow remains:

```text
Calibrate baseline → change scene → Re-test vs Baseline → inspect diagnosis/graph
```

Interactive and headless baselines remain intentionally separate because Blender's interactive and `--background` execution environments are not assumed to have identical timing characteristics.

## Rust CLI

Build from the source checkout:

```powershell
cargo test
cargo build --release
```

### Single asset

```powershell
cargo run -p scenetrace -- baseline "C:\SceneTraceExamples\character.blend"
cargo run -p scenetrace -- test "C:\SceneTraceExamples\character.blend"
```

The single-file layout is:

```text
C:\SceneTraceExamples\.scenetrace\
├── headless-baseline.json
├── headless-latest.json
└── headless.log
```

### Baseline a project

From the project root:

```powershell
cargo run -p scenetrace -- baseline .
```

Example:

```text
SceneTrace 1.0 Project Baseline
Root:     C:\3D\CreatureProject
Blender:  C:\Program Files\Blender Foundation\Blender 4.4\blender.exe
Workers:  2

BASELINE characters/hero.blend
         P95 18.42 ms

BASELINE characters/enemy.blend
         P95 21.17 ms

BASELINE environments/forest.blend
         P95 29.82 ms

3 baselined · 0 errors
Result: BASELINES SAVED
```

### Test a project

```powershell
cargo run -p scenetrace -- test .
```

Example:

```text
SceneTrace 1.0 Project Test
Root:     C:\3D\CreatureProject
Workers:  2

PASS     characters/hero.blend
         P95 18.80 ms · +2.1%

FAIL     characters/enemy.blend
         P95 34.91 ms · +64.9%
         -> Subdivision on Hair_Generator

PASS     environments/forest.blend
         P95 28.95 ms · -2.9%

2 passed · 1 regressions · 0 cached · 0 errors
Result: FAIL
```

A project regression returns exit code `1`. If Blender crashes, a benchmark times out, a baseline is missing, or input/configuration is invalid, the project report includes the affected asset and the CLI returns exit code `2`.

### Changed-only testing

After an asset has passed at least once:

```powershell
cargo run -p scenetrace -- test . --changed
```

SceneTrace streams each `.blend` through BLAKE3 and skips assets whose file hash matches their last passing project test.

```text
PASS     characters/hero.blend
         P95 18.91 ms · +2.7%

SKIP     characters/npc.blend · unchanged
SKIP     environments/city.blend · unchanged
```

**1.0 behavior:** the cache key fingerprints `.blend` bytes plus Blender-reported dependency paths and state. A modified or removed recorded dependency triggers a re-test.

### Parallel workers

Project mode defaults to at most two concurrent Blender processes. Override it with:

```powershell
cargo run -p scenetrace -- test . --workers 3
```

Blender is memory-heavy, so more workers are not automatically better. Every asset has its own `headless.log`, and one worker timeout/failure is captured as an asset-level ERROR rather than terminating sibling Blender processes.

### Project configuration

Create `.scenetrace/config.json`:

```json
{
  "project": {
    "include": ["**/*.blend"],
    "exclude": ["archive/**", "**/backup/**"]
  },
  "frames": {
    "start": 1,
    "end": 100,
    "step": 1
  },
  "benchmark": {
    "warmups": 1,
    "repetitions": 3,
    "calibration_runs": 4,
    "capture_modifier_timings": true
  },
  "budget": {
    "regression_percent": 20,
    "min_delta_ms": 2,
    "target_fps": 30
  },
  "workers": {
    "max_parallel": 2,
    "timeout_seconds": 900
  }
}
```

CLI flags override the relevant config values. When frame start/end are omitted, each Blender scene's own frame range is used.

### JSON report

```powershell
cargo run -p scenetrace -- test . --json
```

The report contains every asset's status, P95, regression delta, pattern, confidence, likely source, file hash, and error when applicable.

### Markdown / GitHub Actions summary

Write a report locally:

```powershell
cargo run -p scenetrace -- test . --markdown scenetrace-report.md
```

Inside GitHub Actions:

```yaml
- name: SceneTrace project performance
  run: scenetrace test . --changed --github-summary
```

`--github-summary` appends a Markdown table to `GITHUB_STEP_SUMMARY`.

## Interactive JSON comparison

The older comparison command remains available for the Blender add-on's interactive files:

```powershell
cargo run -p scenetrace -- compare "C:\SceneTraceExamples\.scenetrace"
```

This compares `baseline.json` with `latest.json`; it does not mix them with headless project baselines.

## Blender discovery

SceneTrace tries, in order:

1. `--blender-path`;
2. `BLENDER_PATH` environment variable;
3. `blender` / `blender.exe` on PATH;
4. common Windows `Blender Foundation` installation directories.

## Measurement model

SceneTrace measures wall-clock time for frame/dependency-graph evaluation. It does not claim to measure complete viewport FPS or render time.

Blender's `Modifier.execution_time` is captured as attribution evidence where available. Modifier timings are deliberately **not summed**, because parallel evaluation can make them overlap.

SceneTrace reports likely contributors as evidence/correlation, not guaranteed causality.

## Why Rust + Python

Blender-specific measurement stays in Python because `bpy` is the natural Blender API. Rust owns the external control plane:

```text
SceneTrace CLI · Rust
├── project discovery
├── BLAKE3 hashing/cache
├── worker scheduling
├── Blender process isolation/timeouts
├── deterministic regression engine
└── CI/report aggregation
        │
        ├── Blender --background → Python probe
        ├── Blender --background → Python probe
        └── Blender --background → Python probe
```

This is the point where the Rust layer does more than parse two JSON files.

## Repository layout

```text
blender/scenetrace/        Blender add-on + shared benchmark code
  headless.py              Blender --background entry point
crates/scenetrace-core/    deterministic Rust regression engine
crates/scenetrace-cli/     discovery/cache/workers/CLI orchestration
tests/                     Python reference tests
fixtures/                  example benchmark data/config
scripts/                   packaging helpers
```

## Original project-runner goal

The release target is:

> **One command can performance-test an entire Blender project without needlessly re-running unchanged `.blend` files.**

## 1.0 release contract

SceneTrace 1.0 hardens the established project runner into a reproducible CI tool. It records versioned artifacts and benchmark environment details, launches Blender with clean factory startup, fingerprints Blender-reported dependencies for `--changed`, and uses atomic metadata writes that preserve prior valid data during Windows file-lock failures.

### Installation and supported Blender

Release archives provide Windows, Linux, and macOS CLI binaries plus SHA-256 checksums; Rust is only needed for source builds. Install `scenetrace-blender-1.0.0.zip` in Blender 4.2+ for the interactive extension. The supported headless CLI/CI version for 1.0 is Blender 4.4.

```powershell
cargo build --release
.\target\release\scenetrace --version
```

Set `BLENDER_PATH` or pass `--blender-path` when Blender is not on `PATH`.

### CI and exit codes

`--json` is the machine contract. Project reports use `scenetrace-project-report` schema version 1 and standalone comparisons use `scenetrace-cli-comparison` schema version 1.

| Exit code | Meaning |
|---:|---|
| 0 | The requested valid checks passed. |
| 1 | A valid benchmark regression was detected. |
| 2 | SceneTrace could not perform a valid check (configuration, Blender, timeout, artifact, or environment failure). |

The included GitHub Actions workflow downloads Blender 4.4 on Linux and Windows, generates a demo scene, verifies an unchanged pass, and verifies a known regression exits 1.

### Environment compatibility

Every headless artifact records SceneTrace and Blender versions, OS, architecture, factory-startup mode, measurement mode, benchmark settings, renderer, and Blender-reported dependencies. Blender major/minor version, startup mode, measurement mode, or benchmark-settings changes are **incompatible** and exit 2 instead of becoming regressions. OS and architecture changes remain **warnings**. Headless operations use `--factory-startup --background`, so user add-ons and preferences do not contaminate measurements.

### Dependencies, noise, and baselines

`--changed` fingerprints the `.blend` bytes plus the baseline’s Blender-reported dependency set (linked libraries, images, sounds, movie clips, caches, and volumes). Existing dependencies use a full content hash; a missing dependency has a distinct fingerprint state. Newly referenced dependencies are recorded when a baseline is created.

Baselines hold repeated samples, noise estimates, thresholds, frame evidence, confidence, persistent/localized classification, and conservative modifier attribution. A result must exceed both percentage and absolute thresholds after calibrated noise is considered. Regenerate a baseline intentionally if the expected performance target or execution environment changes.

All SceneTrace metadata is versioned. Readers accept legacy identifiable baseline envelopes, reject unknown/newer schemas without reinterpretation, and preserve existing files. Cache writes use unique same-directory temporary files, flush/sync, replacement retries for transient Windows locks, and warnings because the cache is disposable; baselines are not silently replaced after a persistence failure.

### Demo

```powershell
$demo = "$env:TEMP\scenetrace-demo"
& $env:BLENDER_PATH --factory-startup --background --python scripts/generate_demo.py -- "$demo\demo.blend"
cargo run -p scenetrace -- baseline $demo --workers 1 --json
cargo run -p scenetrace -- test $demo --workers 1 --json
& $env:BLENDER_PATH --factory-startup --background --python scripts/generate_demo.py -- "$demo\demo.blend" --regression
cargo run -p scenetrace -- test $demo --workers 1 --json  # exits 1
```

Regenerate without `--regression` to restore the passing scene. The deliberate animated subdivision modifier is designed to surface as the likely contributor when Blender timing evidence is available.

See [measurement details](docs/MEASUREMENT.md), [test protocol](TESTING.md), and the [1.0 roadmap](docs/PLAN.md). Release signing/notarization is deferred until project-owned credentials are available.
