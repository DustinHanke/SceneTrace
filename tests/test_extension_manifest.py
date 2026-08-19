from pathlib import Path
import tomllib


def test_extension_manifest_excludes_headless_runner() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "blender" / "scenetrace" / "blender_manifest.toml"
    with manifest_path.open("rb") as manifest_file:
        manifest = tomllib.load(manifest_file)

    assert "headless.py" in manifest["build"]["paths_exclude_pattern"]
