import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

fake_bpy = types.ModuleType("bpy")
fake_bpy.types = types.SimpleNamespace(Scene=object)
sys.modules.setdefault("bpy", fake_bpy)

spec = importlib.util.spec_from_file_location(
    "scenetrace_snapshot", ROOT / "blender" / "scenetrace" / "snapshot.py"
)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class FakeRNA:
    properties = []


class ModifierWithoutIDProperties:
    name = "Armature"
    type = "ARMATURE"
    bl_rna = FakeRNA()

    def keys(self):
        raise TypeError("bpy_struct.keys(): this type doesn't support IDProperties")


class ModifierWithIDProperties:
    name = "GeometryNodes"
    type = "NODES"
    bl_rna = FakeRNA()

    def keys(self):
        return ["Socket_2", "_RNA_UI"]

    def __getitem__(self, key):
        if key == "Socket_2":
            return 0.75
        raise KeyError(key)


def test_modifier_without_idproperties_does_not_abort_snapshot():
    state = snapshot._modifier_state(ModifierWithoutIDProperties())
    assert state["name"] == "Armature"
    assert state["type"] == "ARMATURE"
    assert state["properties"] == {}


def test_modifier_idproperty_input_is_still_recorded():
    state = snapshot._modifier_state(ModifierWithIDProperties())
    assert state["properties"]["input:Socket_2"] == 0.75


def test_semantic_diff_detects_geometry_growth_and_removed_subdivision():
    baseline = {
        "objects": {
            "head.004": {
                "type": "MESH",
                "location": [0, 0, 0], "rotation_euler": [0, 0, 0], "scale": [1, 1, 1],
                "hide_viewport": False, "hide_render": False,
                "mesh": {"vertices": 1000, "edges": 2000, "polygons": 1000, "triangles": 2000, "shape_key_count": 0, "vertex_group_count": 10, "material_slots": []},
                "modifiers": [
                    {"index": 0, "name": "Subdivision", "type": "SUBSURF", "persistent_uid": 0, "properties": {"levels": 2}}
                ],
            }
        }
    }
    current = {
        "objects": {
            "head.004": {
                "type": "MESH",
                "location": [0, 0, 0], "rotation_euler": [0, 0, 0], "scale": [1, 1, 1],
                "hide_viewport": False, "hide_render": False,
                "mesh": {"vertices": 16000, "edges": 32000, "polygons": 16000, "triangles": 32000, "shape_key_count": 0, "vertex_group_count": 10, "material_slots": []},
                "modifiers": [],
            }
        }
    }
    changes = snapshot.diff_snapshots(baseline, current)
    kinds = [change["kind"] for change in changes]
    assert "possible_modifier_applied" in kinds
    assert "geometry" in kinds
    likely = next(c for c in changes if c["kind"] == "possible_modifier_applied")
    assert likely["entity"] == "head.004"
    assert likely["modifier_type"] == "SUBSURF"


def test_modifier_property_diff_is_readable():
    baseline = {"objects": {"Cube": {"type": "MESH", "location": [], "rotation_euler": [], "scale": [], "hide_viewport": False, "hide_render": False, "modifiers": [{"index": 0, "name": "Subdivision", "type": "SUBSURF", "persistent_uid": 0, "properties": {"levels": 1}}]}}}
    current = {"objects": {"Cube": {"type": "MESH", "location": [], "rotation_euler": [], "scale": [], "hide_viewport": False, "hide_render": False, "modifiers": [{"index": 0, "name": "Subdivision", "type": "SUBSURF", "persistent_uid": 0, "properties": {"levels": 3}}]}}}
    changes = snapshot.diff_snapshots(baseline, current)
    prop = next(c for c in changes if c["kind"] == "modifier_property")
    assert prop["label"] == "Subdivision · levels"
    assert prop["before"] == 1
    assert prop["after"] == 3


def test_semantic_diff_detects_evaluated_geometry_growth():
    baseline = {
        "objects": {
            "Head": {
                "type": "MESH",
                "location": [0, 0, 0], "rotation_euler": [0, 0, 0], "scale": [1, 1, 1],
                "hide_viewport": False, "hide_render": False,
                "mesh": {"vertices": 1000, "edges": 2000, "polygons": 1000, "triangles": 2000, "shape_key_count": 0, "vertex_group_count": 0, "material_slots": []},
                "evaluated_mesh": {"vertices": 1000, "edges": 2000, "polygons": 1000, "triangles": 2000},
                "modifiers": [],
            }
        }
    }
    current = {
        "objects": {
            "Head": {
                "type": "MESH",
                "location": [0, 0, 0], "rotation_euler": [0, 0, 0], "scale": [1, 1, 1],
                "hide_viewport": False, "hide_render": False,
                "mesh": {"vertices": 1000, "edges": 2000, "polygons": 1000, "triangles": 2000, "shape_key_count": 0, "vertex_group_count": 0, "material_slots": []},
                "evaluated_mesh": {"vertices": 16000, "edges": 32000, "polygons": 16000, "triangles": 32000},
                "modifiers": [{"index": 0, "name": "Subdivision", "type": "SUBSURF", "persistent_uid": 0, "properties": {"levels": 2}}],
            }
        }
    }
    changes = snapshot.diff_snapshots(baseline, current)
    evaluated = [c for c in changes if c["kind"] == "evaluated_geometry"]
    assert evaluated
    assert any(c["label"] == "Evaluated Vertices" and c["after"] == 16000 for c in evaluated)


def test_numeric_jitter_is_ignored():
    assert snapshot._values_equal(1.0, 1.000001)
    assert snapshot._values_equal([3.56784, 2.605632], [3.567842, 2.605601], abs_tol=1e-4)
    assert not snapshot._values_equal(1.0, 1.02)


def test_animated_object_transform_is_not_reported_as_scene_edit():
    baseline = {
        "objects": {
            "Head": {
                "type": "MESH",
                "animated_paths": ["location"],
                "location": [0.0, 0.0, 0.0], "rotation_euler": [0, 0, 0], "scale": [1, 1, 1],
                "hide_viewport": False, "hide_render": False, "modifiers": [],
            }
        }
    }
    current = {
        "objects": {
            "Head": {
                "type": "MESH",
                "animated_paths": ["location"],
                "location": [2.0, 0.0, 0.0], "rotation_euler": [0, 0, 0], "scale": [1, 1, 1],
                "hide_viewport": False, "hide_render": False, "modifiers": [],
            }
        }
    }
    changes = snapshot.diff_snapshots(baseline, current)
    assert not any(c.get("label") == "Location" for c in changes)


def test_animated_modifier_property_is_not_reported_as_manual_change():
    path = 'modifiers["Wave"].speed'
    baseline = {
        "objects": {
            "Grass": {
                "type": "MESH", "animated_paths": [path],
                "location": [], "rotation_euler": [], "scale": [],
                "hide_viewport": False, "hide_render": False,
                "modifiers": [{
                    "index": 0, "name": "Wave", "type": "WAVE", "persistent_uid": 0,
                    "properties": {"speed": 0.25}, "property_paths": {"speed": path},
                }],
            }
        }
    }
    current = {
        "objects": {
            "Grass": {
                "type": "MESH", "animated_paths": [path],
                "location": [], "rotation_euler": [], "scale": [],
                "hide_viewport": False, "hide_render": False,
                "modifiers": [{
                    "index": 0, "name": "Wave", "type": "WAVE", "persistent_uid": 0,
                    "properties": {"speed": 0.80}, "property_paths": {"speed": path},
                }],
            }
        }
    }
    changes = snapshot.diff_snapshots(baseline, current)
    assert not any(c.get("kind") == "modifier_property" for c in changes)
