# SceneTrace 1.0 Test Protocol

1.0 proves that SceneTrace can orchestrate an entire Blender project from Rust while preserving trustworthy single-scene behavior, stable artifacts, and clean Blender execution.

## 1. Rust tests

From the source root:

```powershell
cargo test
```

The CLI tests now cover config precedence, project discovery, project-relative asset storage keys, and BLAKE3 file hashing in addition to the regression-engine tests.

Then build an optimized binary:

```powershell
cargo build --release
```

## 2. Confirm single-scene compatibility

Use a disposable example scene:

```powershell
cargo run -p scenetrace -- test "C:\SceneTraceExamples\character.blend"
```

An unchanged scene should still PASS against its existing single-file `headless-baseline.json`.

## 3. Create a small project fixture

Use copies of real `.blend` files rather than fake files, for example:

```text
C:\SceneTraceProjectTest\
├── hero.blend
├── characters\
│   └── character.blend
└── archive\
    └── old.blend
```

Create:

```text
C:\SceneTraceProjectTest\.scenetrace\config.json
```

with:

```json
{
  "project": {
    "include": ["**/*.blend"],
    "exclude": ["archive/**"]
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
  "workers": {
    "max_parallel": 2,
    "timeout_seconds": 900
  }
}
```

## 4. Baseline the project

```powershell
cargo run -p scenetrace -- baseline "C:\SceneTraceProjectTest"
```

Expected:

- only the included, non-excluded `.blend` files run;
- two Blender processes may run concurrently;
- each asset receives a distinct directory under `.scenetrace\assets`;
- `archive\old.blend` is absent from the report;
- `.scenetrace\cache.json` is created;
- the command exits `0` if every baseline succeeds.

Inspect:

```powershell
Get-ChildItem -Recurse "C:\SceneTraceProjectTest\.scenetrace\assets"
```

Each asset slot should contain `headless-baseline.json` and `headless.log`.

## 5. First project test

```powershell
cargo run -p scenetrace -- test "C:\SceneTraceProjectTest"
```

All unchanged scenes should normally PASS within their calibrated variance.

## 6. Verify changed-only caching

After the successful project test:

```powershell
cargo run -p scenetrace -- test "C:\SceneTraceProjectTest" --changed
```

Expected: every unchanged `.blend` reports `SKIP ... unchanged` and Blender does not need to benchmark it again.

Now modify and save exactly one `.blend` file, then repeat the same command. Only that file should be benchmarked; the other assets should remain cached.

Important: 1.0 fingerprints the `.blend` file and the dependencies Blender reported when its baseline was made. Verify that changing or removing a linked library/texture/cache causes that scene to be retested.

## 7. Regression test

On one test asset, add the expensive Subdivision change used in previous SceneTrace validation, save it, and run:

```powershell
cargo run -p scenetrace -- test "C:\SceneTraceProjectTest" --changed
```

Expected:

- modified asset: `FAIL`;
- likely source identifies the affected Subdivision/object when Blender timing evidence supports it;
- unchanged scenes: `SKIP`;
- project result: `FAIL`;
- process exit code: `1`.

```powershell
$LASTEXITCODE
```

should print `1`.

## 8. Worker isolation / error semantics

Set a deliberately tiny timeout:

```powershell
cargo run -p scenetrace -- test "C:\SceneTraceProjectTest" --workers 2 --timeout-seconds 1
```

At least one sufficiently complex asset should become `ERROR` if Blender cannot finish in time. Other worker results should still be collected. A project containing benchmark execution errors returns exit code `2`, not regression exit code `1`.

## 9. Markdown report

```powershell
cargo run -p scenetrace -- test "C:\SceneTraceProjectTest" --markdown "C:\SceneTraceProjectTest\report.md"
```

Open `report.md` and confirm it contains an asset table with status, P95, change, and likely source.

## 10. JSON report

```powershell
cargo run -p scenetrace -- test "C:\SceneTraceProjectTest" --json
```

The JSON should have schema `scenetrace-project-report`, the project counts, worker count, changed-only state, and an `assets` array.

## 11. GitHub Actions summary locally (optional)

PowerShell can simulate `GITHUB_STEP_SUMMARY`:

```powershell
$env:GITHUB_STEP_SUMMARY = "C:\SceneTraceProjectTest\github-summary.md"
cargo run -p scenetrace -- test "C:\SceneTraceProjectTest" --github-summary
```

Then inspect the generated Markdown file.

## 12. Blender extension regression check

Install `scenetrace-blender-1.0.0.zip` and confirm the interactive workflow still works:

- calibrated interactive baseline;
- stable unchanged run;
- likely contributor diagnosis;
- region-aware graph placement around T/N panels.

The project-runner additions preserve interactive benchmark semantics. Also run `scripts/generate_demo.py` through real Blender to verify baseline, unchanged PASS, deliberate regression exit 1, restore, and PASS as documented in the README.
