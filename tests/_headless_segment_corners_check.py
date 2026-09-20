"""Headless smoke test: register the extension and exercise the Define
Corners / Manage Segments additions to the Path Segment authoring tool in
real bpy. Run with:
    blender --background --python tests/_headless_segment_corners_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.

Mirrors tests/_headless_segment_add_check.py's approach: the interactive
pieces (mouse-driven Add Corner clicks, the Manage Segments popup's own
draw()) can't be driven headlessly, so this script builds synthetic RNA
state directly and drives the underlying operators — HEXFINITY_OT_
finish_add_segment's new corners gate, HEXFINITY_OT_snap_corner_to_edge,
and the Manage Segments delete/move operators — plus settings.json
read/write. A trailing comment block lists the interactive-only steps that
still need a manual click-through.
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

# Same throwaway-settings-file redirection as _headless_segment_add_check.py.
_tmp_dir = tempfile.mkdtemp(prefix="hexfinity_segment_corners_check_")
_tmp_settings_path = os.path.join(_tmp_dir, "settings.json")
segments._settings_path = lambda: _tmp_settings_path

hexfinity.register()
print("register() OK")

scene = bpy.context.scene


def make_square_segment(name, source_filepath, type_name):
    """Same synthetic-STL-standin shape as _headless_segment_add_check.py's
    helper, but deliberately leaves seg.corners empty by default so each
    section here can populate it exactly as needed."""
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
    seg.corners.clear()
    seg.waypoints.clear()
    seg.has_drawn_path = False
    return obj


# ---------------------------------------------------------------------------
# 1. Finish refuses a path with zero corners, independently of the
#    pre-existing zero-edge-waypoint gate (both must hold). poll() now
#    gates on corners too, so bpy.ops raises its own generic "poll()
#    failed" RuntimeError before ever reaching invoke()'s specific
#    "3 corners" message (which is checked directly below as the
#    defense-in-depth path poll() would otherwise mask).

obj = make_square_segment("SegA", "C:/segs/Bridge/a.stl", "Bridge")
scene.hexfinity_segments.active_object = obj
scene.hexfinity_segments.edge_snap = 3

seg = obj.hexfinity_segment
for (x, y, edge_idx) in [(-10.0, 0.0, 3), (10.0, 0.0, 1)]:
    wp = seg.waypoints.add()
    wp.x, wp.y, wp.edge_idx = x, y, edge_idx
seg.has_drawn_path = True

assert len(seg.corners) == 0
assert segments.HEXFINITY_OT_finish_add_segment.poll(bpy.context) is False
try:
    bpy.ops.hexfinity.finish_add_segment()
    raise AssertionError("expected finish_add_segment to raise (poll failure)")
except RuntimeError as exc:
    assert "poll" in str(exc), exc
data = segment_settings.load_settings(_tmp_settings_path)
assert not segment_settings.has_type(data, "Bridge")
print("finish_add_segment poll() refuses a path with zero corners OK")

# Defense-in-depth: invoke()'s own hard-coded corners check (in case
# something ever calls it directly, bypassing poll) — exercised here by
# temporarily bypassing poll() itself via bpy.ops's real dispatch, rather
# than hand-instantiating the Operator (unsafe/unsupported outside bpy.ops).
_orig_poll = segments.HEXFINITY_OT_finish_add_segment.poll
segments.HEXFINITY_OT_finish_add_segment.poll = classmethod(lambda cls, context: True)
try:
    try:
        bpy.ops.hexfinity.finish_add_segment()
        raise AssertionError("expected finish_add_segment to raise (ERROR report)")
    except RuntimeError as exc:
        assert "3 corners" in str(exc), exc
finally:
    segments.HEXFINITY_OT_finish_add_segment.poll = _orig_poll
data = segment_settings.load_settings(_tmp_settings_path)
assert not segment_settings.has_type(data, "Bridge")
print("finish_add_segment.invoke()'s own hard-coded corners check OK")

# ---------------------------------------------------------------------------
# 2. Populate corners via RNA and drive Snap to Edge through the real
#    operator; it must land on the hull's boundary.

for (x, y) in [(-9.0, -4.0), (9.0, -4.0), (9.0, 4.0)]:
    c = seg.corners.add()
    c.x, c.y = x, y
seg.active_corner_index = 0

result = bpy.ops.hexfinity.snap_corner_to_edge()
assert result == {'FINISHED'}, result
c0 = seg.corners[0]
assert c0.y == -5.0, c0.y  # snapped onto the bottom hull edge (y = -5)
assert c0.edge_idx == 0, c0.edge_idx
assert c0.origin == 'HULL_EDGE', c0.origin
print("snap_corner_to_edge lands on the expected hull edge OK")

# lock_x + lock_y both true -> no-op, reported (WARNING, not ERROR, so
# bpy.ops returns normally rather than raising) and cancelled.
c0.lock_x = True
c0.lock_y = True
before = (c0.x, c0.y, c0.edge_idx)
result = bpy.ops.hexfinity.snap_corner_to_edge()
assert result == {'CANCELLED'}, result
assert (c0.x, c0.y, c0.edge_idx) == before
c0.lock_x = False
c0.lock_y = False
print("snap_corner_to_edge refuses when both axes are locked OK")

# ---------------------------------------------------------------------------
# 3. Both gates satisfied -> Finish writes corners_local_mm alongside
#    hull_local_mm/waypoints.

assert len(seg.corners) == 3
result = bpy.ops.hexfinity.finish_add_segment()
assert result == {'FINISHED'}, result

data = segment_settings.load_settings(_tmp_settings_path)
entry = segment_settings.find_segment(data, "Bridge", "C:/segs/Bridge/a.stl")
assert entry is not None
assert len(entry["hull_local_mm"]) == 4
assert entry["corners_local_mm"] == [
    [-9.0, -5.0], [9.0, -4.0], [9.0, 4.0],
]
print("finish_add_segment writes corners_local_mm alongside hull_local_mm OK")

# ---------------------------------------------------------------------------
# 4. Manage Segments delete/reorder operators, driven directly (the
#    dialog's own draw() can't be exercised headlessly).

obj2 = make_square_segment("SegB", "C:/segs/Bridge/b.stl", "Bridge")
scene.hexfinity_segments.active_object = obj2
seg2 = obj2.hexfinity_segment
for (x, y) in [(-9.0, -4.0), (9.0, -4.0), (9.0, 4.0)]:
    c = seg2.corners.add()
    c.x, c.y = x, y
for (x, y, edge_idx) in [(-10.0, 0.0, 3), (10.0, 0.0, 1)]:
    wp = seg2.waypoints.add()
    wp.x, wp.y, wp.edge_idx = x, y, edge_idx
seg2.has_drawn_path = True
result = bpy.ops.hexfinity.finish_add_segment()
assert result == {'FINISHED'}, result

data = segment_settings.load_settings(_tmp_settings_path)
segs = segment_settings.list_segments(data, "Bridge")
assert [s["file"] for s in segs] == ["C:/segs/Bridge/a.stl", "C:/segs/Bridge/b.stl"]
print("type accumulates multiple segments OK")

result = bpy.ops.hexfinity.move_segment_entry(type_name="Bridge", index=0, direction=1)
assert result == {'FINISHED'}, result
data = segment_settings.load_settings(_tmp_settings_path)
segs = segment_settings.list_segments(data, "Bridge")
assert [s["file"] for s in segs] == ["C:/segs/Bridge/b.stl", "C:/segs/Bridge/a.stl"]
print("move_segment_entry reorders settings.json immediately OK")

result = bpy.ops.hexfinity.remove_segment_entry(type_name="Bridge", index=0)
assert result == {'FINISHED'}, result
data = segment_settings.load_settings(_tmp_settings_path)
segs = segment_settings.list_segments(data, "Bridge")
assert [s["file"] for s in segs] == ["C:/segs/Bridge/a.stl"]
print("remove_segment_entry deletes from settings.json immediately OK")

# The Manage Segments dialog itself opens fine (INVOKE_DEFAULT would try to
# pop up a real window in --background mode and fail; poll() + the enum
# items callback are the only headlessly-checkable pieces).
assert segments.HEXFINITY_OT_manage_segments.poll(bpy.context)  # no workflow object active
items = segments._type_enum_items(None, bpy.context)
assert ("Bridge", "Bridge", "") in items
print("manage_segments poll()/type enum items OK")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS SEGMENT CORNERS CHECK PASSED")

# ---------------------------------------------------------------------------
# Manual-only checklist (not scriptable headlessly — requires a real
# viewport and mouse input):
#
# 1. Import a concave/L-shaped test STL via "Add Path Segment Type".
# 2. Click "Add Corner" repeatedly around its footprint; confirm sharp
#    hover-color hints (orange) appear distinctly from plain hull-edge
#    snap hints (green) near the hull's genuinely sharp vertices, and not
#    near a rounded/filleted section of the same hull.
# 3. Once >= 3 corners are placed, start "Draw Path" and confirm it now
#    snaps against the corners polygon (including the new corner-to-corner
#    midpoints), not the original convex hull, and that clicking outside
#    the corners polygon (but still inside the convex hull) is rejected.
# 4. Open "Manage Segments" from the Settings box with no workflow active;
#    confirm the type dropdown lists existing types, selecting one lists
#    its segments, and Delete/Move Up/Move Down each immediately update
#    the list and reopen the popup.
# 5. Confirm "Finish Add Segment" stays greyed out until both the
#    waypoint-edge and corners-count (>= 3) requirements are met.
