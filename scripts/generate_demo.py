from __future__ import annotations

import sys
from pathlib import Path

import bpy


def arguments() -> tuple[Path, bool]:
    raw = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if not raw:
        raise RuntimeError("usage: blender --background --python generate_demo.py -- <output.blend> [--regression]")
    return Path(raw[0]).resolve(), "--regression" in raw[1:]


def build(output: Path, regression: bool) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 3
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.name = "DemoCube"
    if regression:
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=120, y_subdivisions=120)
        grid = bpy.context.active_object
        grid.name = "RegressionGrid"
        modifier = grid.modifiers.new("Deliberate Subdivision Regression", "SUBSURF")
        modifier.levels = 1
        modifier.keyframe_insert(data_path="levels", frame=1)
        modifier.levels = 3
        modifier.keyframe_insert(data_path="levels", frame=2)
        modifier.levels = 1
        modifier.keyframe_insert(data_path="levels", frame=3)
        modifier.render_levels = 3
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)


def main() -> None:
    output, regression = arguments()
    build(output, regression)


if __name__ == "__main__":
    main()
