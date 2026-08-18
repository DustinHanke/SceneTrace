# Changelog

## 1.0.2

- License the Blender extension under GPL-3.0-or-later for Blender Extensions compatibility.
- Add extension SPDX headers and a bundled GPL notice.
- Package a Blender Extensions-compliant manifest with a valid files permission reason.

## 1.0.1

- Include the Blender headless runner in standalone CLI archives so released binaries work outside a source checkout.
- Verify packaged archives contain both the CLI executable and `headless.py` before publishing.

## 1.0.0

- Isolated headless Blender benchmarks with factory startup.
- Added versioned cache and headless artifact metadata.
- Added dependency-aware project fingerprints for Blender-reported external files.
- Hardened metadata replacement against predictable temp-file collisions and transient Windows locks.
- Added environment compatibility classification to distinguish invalid comparisons from regressions.
- Added reproducible demo generation and CI release checks.
