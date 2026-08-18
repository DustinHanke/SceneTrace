# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any
import math

import bpy


def _round(value: float) -> float:
    return round(float(value), 6)


def _scalar(value: Any):
    if isinstance(value, (bool, int, float, str)):
        return value if not isinstance(value, float) else _round(value)
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        try:
            vals = list(value)
        except Exception:
            return None
        if len(vals) <= 4 and all(isinstance(v, (bool, int, float)) for v in vals):
            return [(_round(v) if isinstance(v, float) else v) for v in vals]
    return None


def _escape_identifier(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _modifier_property_path(modifier_name: str, prop: str) -> str:
    name = _escape_identifier(modifier_name)
    if prop.startswith("input:"):
        key = _escape_identifier(prop.split(":", 1)[1])
        return f'modifiers["{name}"]["{key}"]'
    return f'modifiers["{name}"].{prop}'


def _collect_action_paths(action, paths: set[str]):
    if action is None:
        return
    try:
        curves = list(action.fcurves)
    except Exception:
        curves = []
    for curve in curves:
        try:
            paths.add(str(curve.data_path))
        except Exception:
            pass


def _animated_data_paths(obj) -> list[str]:
    """Best-effort list of properties driven/keyframed on this object.

    SceneTrace uses this to avoid treating evaluated animation values as manual
    scene edits. Unsupported/new Action layouts simply fall back to the
    deterministic reference-frame snapshot rather than aborting analysis.
    """
    paths: set[str] = set()
    animation_data = getattr(obj, "animation_data", None)
    if animation_data is None:
        return []

    try:
        for curve in animation_data.drivers:
            paths.add(str(curve.data_path))
    except Exception:
        pass

    _collect_action_paths(getattr(animation_data, "action", None), paths)

    try:
        for track in animation_data.nla_tracks:
            for strip in track.strips:
                _collect_action_paths(getattr(strip, "action", None), paths)
    except Exception:
        pass

    return sorted(paths)


def _is_animated(path: str, *states: dict) -> bool:
    for state in states:
        if not state:
            continue
        animated = set(state.get("animated_paths", []) or [])
        if path in animated:
            return True
    return False


def _values_equal(before, after, *, abs_tol: float = 1e-5, rel_tol: float = 5e-4) -> bool:
    """Compare snapshot values without surfacing insignificant float jitter."""
    if before is None or after is None:
        return before is after
    if isinstance(before, bool) or isinstance(after, bool):
        return before == after
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return math.isclose(float(before), float(after), rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        if len(before) != len(after):
            return False
        return all(_values_equal(b, a, abs_tol=abs_tol, rel_tol=rel_tol) for b, a in zip(before, after))
    return before == after


def _modifier_state(mod, index: int = 0) -> dict:
    props = {}
    property_paths = {}
    derived_or_ui_only = {
        "rna_type", "name", "type", "execution_time", "is_active",
        "persistent_uid", "show_expanded",
    }
    for prop in getattr(mod.bl_rna, "properties", []):
        ident = getattr(prop, "identifier", "")
        if not ident or ident in derived_or_ui_only or getattr(prop, "is_readonly", False):
            continue
        try:
            value = _scalar(getattr(mod, ident))
        except Exception:
            continue
        if value is not None:
            props[ident] = value
            property_paths[ident] = _modifier_property_path(mod.name, ident)
    try:
        id_property_keys = list(mod.keys())
    except (AttributeError, TypeError, RuntimeError):
        id_property_keys = []

    for key in id_property_keys:
        if key == "_RNA_UI":
            continue
        try:
            value = _scalar(mod[key])
        except (KeyError, TypeError, RuntimeError):
            continue
        if value is not None:
            prop_name = f"input:{key}"
            props[prop_name] = value
            property_paths[prop_name] = _modifier_property_path(mod.name, prop_name)

    persistent_uid = 0
    try:
        persistent_uid = int(mod.persistent_uid)
    except Exception:
        pass
    return {
        "index": index,
        "name": mod.name,
        "type": mod.type,
        "persistent_uid": persistent_uid,
        "properties": props,
        "property_paths": property_paths,
    }


def _mesh_counts(mesh) -> dict:
    triangles = 0
    try:
        mesh.calc_loop_triangles()
        triangles = len(mesh.loop_triangles)
    except Exception:
        pass
    return {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "polygons": len(mesh.polygons),
        "triangles": triangles,
    }


def _mesh_state(obj) -> dict:
    mesh = obj.data
    counts = _mesh_counts(mesh)

    shape_keys = []
    try:
        if mesh.shape_keys:
            shape_keys = [key.name for key in mesh.shape_keys.key_blocks]
    except Exception:
        pass

    materials = []
    try:
        materials = [slot.material.name if slot.material else None for slot in obj.material_slots]
    except Exception:
        pass

    vertex_groups = []
    try:
        vertex_groups = [group.name for group in obj.vertex_groups]
    except Exception:
        pass

    return {
        "data_name": mesh.name,
        **counts,
        "shape_key_count": len(shape_keys),
        "shape_keys": shape_keys,
        "vertex_group_count": len(vertex_groups),
        "vertex_groups": vertex_groups,
        "material_slots": materials,
    }


def _evaluated_mesh_state(obj, depsgraph) -> dict | None:
    """Capture evaluated mesh complexity outside the benchmark timer.

    This lets SceneTrace distinguish unchanged base topology from a modifier that
    dramatically increases the evaluated geometry. It is diagnostic metadata,
    not part of the measured frame wall time.
    """
    if depsgraph is None or obj.type != "MESH":
        return None
    try:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.data
        if mesh is None or not hasattr(mesh, "vertices"):
            return None
        return _mesh_counts(mesh)
    except Exception:
        return None


def build_scene_snapshot(scene: bpy.types.Scene, depsgraph=None) -> dict:
    objects = {}
    for obj in scene.objects:
        state = {
            "type": obj.type,
            "animated_paths": _animated_data_paths(obj),
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "location": [_round(v) for v in obj.location],
            "rotation_euler": [_round(v) for v in obj.rotation_euler],
            "scale": [_round(v) for v in obj.scale],
            "modifiers": [_modifier_state(mod, i) for i, mod in enumerate(obj.modifiers)],
        }
        if obj.type == "MESH" and obj.data:
            state["mesh"] = _mesh_state(obj)
            evaluated = _evaluated_mesh_state(obj, depsgraph)
            if evaluated is not None:
                state["evaluated_mesh"] = evaluated
        objects[obj.name] = state

    render = scene.render
    return {
        "reference_frame": int(scene.frame_current),
        "objects": objects,
        "scene": {
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "render_engine": scene.render.engine,
            "fps": render.fps,
            "fps_base": render.fps_base,
            "use_simplify": bool(render.use_simplify),
            "simplify_subdivision": int(render.simplify_subdivision),
        },
    }


def _pct(before: float, after: float) -> float:
    if abs(before) < 1e-9:
        return 0.0 if abs(after) < 1e-9 else 100.0
    return (after - before) / before * 100.0


def _change(entity: str, kind: str, label: str, before=None, after=None, priority: int = 50, **extra) -> dict:
    return {
        "entity": entity,
        "kind": kind,
        "label": label,
        "before": before,
        "after": after,
        "priority": priority,
        **extra,
    }


def _modifier_key(mod: dict, fallback_index: int) -> tuple:
    uid = int(mod.get("persistent_uid", 0) or 0)
    if uid:
        return ("uid", uid)
    return ("name", mod.get("name", ""), mod.get("type", ""), fallback_index)


def _diff_modifiers(entity: str, before: list[dict], after: list[dict], before_state: dict | None = None, after_state: dict | None = None) -> list[dict]:
    changes = []
    before_map = {_modifier_key(mod, i): mod for i, mod in enumerate(before)}
    after_map = {_modifier_key(mod, i): mod for i, mod in enumerate(after)}

    if not any(key[0] == "uid" for key in set(before_map) | set(after_map)):
        before_map = {(mod.get("name"), mod.get("type")): mod for mod in before}
        after_map = {(mod.get("name"), mod.get("type")): mod for mod in after}

    for key in before_map.keys() - after_map.keys():
        mod = before_map[key]
        changes.append(_change(
            entity, "modifier_removed", f"Modifier removed: {mod.get('name')} ({mod.get('type')})",
            before=mod.get("name"), after=None, priority=85, modifier=mod,
        ))
    for key in after_map.keys() - before_map.keys():
        mod = after_map[key]
        changes.append(_change(
            entity, "modifier_added", f"Modifier added: {mod.get('name')} ({mod.get('type')})",
            before=None, after=mod.get("name"), priority=85, modifier=mod,
            modifier_name=mod.get("name"), modifier_type=mod.get("type"),
        ))
    for key in before_map.keys() & after_map.keys():
        bmod = before_map[key]
        amod = after_map[key]
        bprops = bmod.get("properties", {})
        aprops = amod.get("properties", {})
        for prop in sorted(set(bprops) | set(aprops)):
            bpath = bmod.get("property_paths", {}).get(prop) or _modifier_property_path(bmod.get("name", ""), prop)
            apath = amod.get("property_paths", {}).get(prop) or _modifier_property_path(amod.get("name", ""), prop)
            # Evaluated values of driven/keyframed properties are not evidence of
            # a manual scene edit. Structural animation changes deserve their own
            # future diff rather than being mixed into performance attribution.
            if _is_animated(bpath, before_state, after_state) or _is_animated(apath, before_state, after_state):
                continue
            if not _values_equal(bprops.get(prop), aprops.get(prop)):
                changes.append(_change(
                    entity,
                    "modifier_property",
                    f"{amod.get('name')} · {prop}",
                    before=bprops.get(prop),
                    after=aprops.get(prop),
                    priority=80,
                    modifier_name=amod.get("name"),
                    modifier_type=amod.get("type"),
                    property=prop,
                ))
        if bmod.get("index") != amod.get("index"):
            changes.append(_change(
                entity, "modifier_reordered", f"Modifier reordered: {amod.get('name')}",
                before=bmod.get("index"), after=amod.get("index"), priority=60,
                modifier_name=amod.get("name"), modifier_type=amod.get("type"),
            ))
    return changes


def _diff_geometry_counts(entity: str, before: dict, after: dict, kind: str, prefix: str, priority_boost: int = 0) -> tuple[list[dict], bool]:
    changes = []
    geometry_growth = False
    for field, label in (
        ("vertices", "Vertices"),
        ("edges", "Edges"),
        ("polygons", "Faces"),
        ("triangles", "Triangles"),
    ):
        b = int(before.get(field, 0) or 0)
        a = int(after.get(field, 0) or 0)
        if b == a:
            continue
        pct = _pct(float(b), float(a))
        if a > b * 1.2 and a - b >= 100:
            geometry_growth = True
        changes.append(_change(
            entity,
            kind,
            f"{prefix}{label}",
            before=b,
            after=a,
            priority=min(110, (95 if abs(pct) >= 50 else 70) + priority_boost),
            delta=a - b,
            delta_percent=pct,
        ))
    return changes, geometry_growth


def _diff_mesh(entity: str, before: dict, after: dict, removed_modifiers: list[dict]) -> list[dict]:
    changes, geometry_growth = _diff_geometry_counts(entity, before, after, "geometry", "")

    for field, label in (
        ("shape_key_count", "Shape keys"),
        ("vertex_group_count", "Vertex groups"),
    ):
        if before.get(field) != after.get(field):
            changes.append(_change(entity, "mesh_structure", label, before.get(field), after.get(field), priority=65))

    if before.get("material_slots") != after.get("material_slots"):
        changes.append(_change(entity, "materials", "Material slots changed", before.get("material_slots"), after.get("material_slots"), priority=55))

    expensive_types = {"SUBSURF", "MULTIRES", "NODES", "REMESH", "BOOLEAN", "SOLIDIFY", "MIRROR", "ARRAY", "SCREW", "SKIN"}
    if geometry_growth:
        for change in removed_modifiers:
            mod = change.get("modifier", {})
            if mod.get("type") in expensive_types:
                bverts = int(before.get("vertices", 0) or 0)
                averts = int(after.get("vertices", 0) or 0)
                ratio = (averts / bverts) if bverts else 0.0
                changes.append(_change(
                    entity,
                    "possible_modifier_applied",
                    f"Likely baked geometry: {mod.get('name')} removed while base mesh grew",
                    before=f"{bverts:,} verts + {mod.get('name')}",
                    after=f"{averts:,} base verts",
                    priority=110,
                    modifier_name=mod.get("name"),
                    modifier_type=mod.get("type"),
                    vertex_ratio=ratio,
                    note="Correlation heuristic: this is consistent with applying/baking the modifier, not proof.",
                ))
    return changes


def diff_snapshots(baseline: dict, current: dict, limit: int = 100) -> list[dict]:
    changes = []
    base_objects = baseline.get("objects", {})
    cur_objects = current.get("objects", {})
    for name in sorted(set(base_objects) | set(cur_objects)):
        before = base_objects.get(name)
        after = cur_objects.get(name)
        if before is None:
            changes.append(_change(name, "object_added", "Object added", None, after.get("type") if after else "added", 60))
            continue
        if after is None:
            changes.append(_change(name, "object_removed", "Object removed", before.get("type"), None, 70))
            continue

        modifier_changes = _diff_modifiers(
            name, before.get("modifiers", []), after.get("modifiers", []), before, after
        )
        changes.extend(modifier_changes)
        if before.get("mesh") and after.get("mesh"):
            removed = [c for c in modifier_changes if c.get("kind") == "modifier_removed"]
            changes.extend(_diff_mesh(name, before["mesh"], after["mesh"], removed))

        # v0.3: evaluated topology exposes complexity introduced by modifiers,
        # even when the base mesh itself has not changed.
        if before.get("evaluated_mesh") and after.get("evaluated_mesh"):
            evaluated_changes, _ = _diff_geometry_counts(
                name,
                before["evaluated_mesh"],
                after["evaluated_mesh"],
                "evaluated_geometry",
                "Evaluated ",
                priority_boost=5,
            )
            changes.extend(evaluated_changes)

        for field, label in (
            ("location", "Location"),
            ("rotation_euler", "Rotation"),
            ("scale", "Scale"),
            ("hide_viewport", "Viewport visibility"),
            ("hide_render", "Render visibility"),
        ):
            if _is_animated(field, before, after):
                continue
            abs_tol = 1e-4 if field in {"location", "rotation_euler", "scale"} else 1e-5
            if not _values_equal(before.get(field), after.get(field), abs_tol=abs_tol):
                changes.append(_change(name, "object_property", label, before.get(field), after.get(field), priority=35))

    changes.sort(key=lambda c: (-int(c.get("priority", 0)), c.get("entity", ""), c.get("label", "")))
    return changes[:limit]
