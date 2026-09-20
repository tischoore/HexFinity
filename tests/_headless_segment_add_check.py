"""Headless smoke test: register the extension and exercise the Path
Segment authoring tool's non-interactive pieces in real bpy. Run with:
    blender --background --python tests/_headless_segment_add_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.

The full interactive flow (file browser, confirm popup, viewport framing,
the Draw Path modal's mouse-driven snapping) can't be driven headlessly —
same caveat as tests/_headless_path_feature_check.py. This script instead
builds a synthetic "imported STL" object directly (mirroring what
HEXFINITY_OT_confirm_add_segment_type.execute() would leave behind) and
drives the underlying operators/data straight through: Finish/Cancel,
settings.json read/write, and dedup.
"""
import os
import sys
import tempfile

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import hexfinity
from hexfinity import segments, segment_geometry, segment_settings

# Redirect settings.json to a throwaway temp file for the duration of this
# script, so a manual run never touches the real per-user extension data --
# patched before register() runs, since register() calls
# segments.ensure_settings_file() itself. This also sidesteps
# bpy.utils.extension_path_user() requiring a real installed-extension
# package name (e.g. "bl_ext.<repo>.hexfinity"), which this script's plain
# sys.path-based `import hexfinity` does not have -- register() tolerates
# that failure (see __init__.py), but patching first lets this script
# actually exercise real settings.json read/write instead of skipping it.
_tmp_dir = tempfile.mkdtemp(prefix="hexfinity_segment_check_")
_tmp_settings_path = os.path.join(_tmp_dir, "settings.json")
segments._settings_path = lambda: _tmp_settings_path

hexfinity.register()
print("register() OK")

# register() already called ensure_settings_file() once (against the
# patched path above), so the file should already exist with a fresh
# default document.
assert os.path.isfile(_tmp_settings_path)
data = segment_settings.load_settings(_tmp_settings_path)
assert data == segment_settings.default_settings()
print("register() -> ensure_settings_file() creates a fresh default file OK")

# ensure_settings_file() must not clobber an existing file on a second call.
segment_settings.set_last_directory(data, "C:/probe")
segment_settings.save_settings(_tmp_settings_path, data)
segments.ensure_settings_file()
assert segment_settings.load_settings(_tmp_settings_path)["last_directory"] == "C:/probe"
print("ensure_settings_file() leaves an existing file untouched OK")

scene = bpy.context.scene


def make_square_segment(name, source_filepath, type_name):
    """A flat 1x1 quad standing in for an imported STL — enough to exercise
    convex_hull + the authoring PropertyGroup without a real file."""
    mesh = bpy.data.meshes.new(name)
    verts = [(-10.0, -5.0, 0.0), (10.0, -5.0, 0.0), (10.0, 5.0, 0.0), (-10.0, 5.0, 0.0)]
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)

    hull = segment_geometry.convex_hull([(v.co.x, v.co.y) for v in mesh.vertices])
    assert len(hull) == 4, hull

    seg = obj.hexfinity_segment
    seg.type_name = type_name
    seg.source_filepath = source_filepath
    seg.type_is_new = True
    seg.hull.clear()
    for (x, y) in hull:
        p = seg.hull.add()
        p.x, p.y = x, y
    # finish_add_segment now also requires >= 3 corners; reuse the same
    # square as the Corners polygon (a real authoring session would place
    # these via Add Corner, but for this non-interactive script the exact
    # geometry doesn't matter, only that the >= 3 gate is satisfied).
    seg.corners.clear()
    for (x, y) in hull:
        c = seg.corners.add()
        c.x, c.y = x, y
    seg.waypoints.clear()
    seg.has_drawn_path = False
    return obj


# ---------------------------------------------------------------------------
# Finish requires at least one edge-snapped waypoint.

obj = make_square_segment("SegA", "C:/segs/Bridge/a.stl", "Bridge")
scene.hexfinity_segments.active_object = obj
scene.hexfinity_segments.edge_snap = 3

seg = obj.hexfinity_segment
for (x, y) in [(-10.0, 0.0), (10.0, 0.0)]:
    wp = seg.waypoints.add()
    wp.x, wp.y, wp.edge_idx = x, y, -1
seg.has_drawn_path = True

try:
    bpy.ops.hexfinity.finish_add_segment()
    raise AssertionError("expected finish_add_segment to raise (ERROR report)")
except RuntimeError as exc:
    assert "edge" in str(exc), exc
data = segment_settings.load_settings(_tmp_settings_path)
assert not segment_settings.has_type(data, "Bridge")
print("finish_add_segment refuses a path with zero edge waypoints OK")

# ---------------------------------------------------------------------------
# One edge waypoint -> is_end_segment True, execute() path (bypassing the
# interactive invoke_confirm warning, same as clicking OK on it).

seg.waypoints.clear()
for (x, y, edge_idx) in [(-10.0, 0.0, 3), (0.0, 0.0, -1)]:
    wp = seg.waypoints.add()
    wp.x, wp.y, wp.edge_idx = x, y, edge_idx

assert scene.hexfinity_segments.active_object is obj
obj_name = obj.name
result = bpy.ops.hexfinity.finish_add_segment()
assert result == {'FINISHED'}, result
assert scene.hexfinity_segments.active_object is None
assert obj_name not in bpy.data.objects
print("finish_add_segment (1 edge waypoint) records + cleans up OK")

data = segment_settings.load_settings(_tmp_settings_path)
assert segment_settings.has_type(data, "Bridge")
entry = segment_settings.find_segment(data, "Bridge", "C:/segs/Bridge/a.stl")
assert entry is not None
assert entry["is_end_segment"] is True, entry
assert entry["man_height_mm"] == segment_settings.DEFAULT_MAN_HEIGHT_MM
assert len(entry["hull_local_mm"]) == 4
assert len(entry["corners_local_mm"]) == 4
assert entry["waypoints"] == [
    {"x_mm": -10.0, "y_mm": 0.0, "z_mm": 0.0, "edge_idx": 3},
    {"x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0, "edge_idx": -1},
]
print("settings.json entry shape OK:", entry["file"], "is_end_segment =", entry["is_end_segment"])

# ---------------------------------------------------------------------------
# Two edge waypoints -> is_end_segment False.

obj2 = make_square_segment("SegB", "C:/segs/Bridge/b.stl", "Bridge")
scene.hexfinity_segments.active_object = obj2
seg2 = obj2.hexfinity_segment
for (x, y, edge_idx) in [(-10.0, 0.0, 3), (10.0, 0.0, 1)]:
    wp = seg2.waypoints.add()
    wp.x, wp.y, wp.edge_idx = x, y, edge_idx
seg2.has_drawn_path = True

result = bpy.ops.hexfinity.finish_add_segment()
assert result == {'FINISHED'}, result
data = segment_settings.load_settings(_tmp_settings_path)
entry2 = segment_settings.find_segment(data, "Bridge", "C:/segs/Bridge/b.stl")
assert entry2["is_end_segment"] is False, entry2
print("finish_add_segment (2 edge waypoints) -> is_end_segment False OK")

# Same type, both segments present now.
type_entry = data["segment_path"]["types"]["Bridge"]
assert len(type_entry["segments"]) == 2, type_entry
print("type accumulates multiple segments OK")

# ---------------------------------------------------------------------------
# Cancel: no settings.json write, object cleaned up.

obj3 = make_square_segment("SegC", "C:/segs/Tunnel/c.stl", "Tunnel")
scene.hexfinity_segments.active_object = obj3
obj3_name = obj3.name
result = bpy.ops.hexfinity.cancel_add_segment()
assert result == {'FINISHED'}, result
assert scene.hexfinity_segments.active_object is None
assert obj3_name not in bpy.data.objects
data = segment_settings.load_settings(_tmp_settings_path)
assert not segment_settings.has_type(data, "Tunnel")
print("cancel_add_segment writes nothing + cleans up OK")

# ---------------------------------------------------------------------------
# Duplicate file under the same type is refused by find_segment/add_segment
# (the same check add_segment_type.execute() runs before opening the
# confirm dialog).

assert segment_settings.find_segment(data, "Bridge", "c:/SEGS/bridge/A.STL") is not None
print("duplicate-file detection (case-insensitive path) OK")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS SEGMENT ADD CHECK PASSED")
