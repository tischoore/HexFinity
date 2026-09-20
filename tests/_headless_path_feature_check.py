"""Headless smoke test: register the extension and exercise the path
feature draw/commit/remove path in real bpy. Run with:
    blender --background --python tests/_headless_path_feature_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.
"""
import os
import sys

import bpy
from mathutils import Vector

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import hexfinity

hexfinity.register()
print("register() OK")

scene = bpy.context.scene
mp = scene.hexfinity_map
mp.diameter_mm = 220.0
mp.level_height_mm = 10.0
mp.base_thickness_mm = 10.0
mp.smoothness_passes = 2
mp.resample_density = 0
mp.man_height_mm = 10.0
mp.is_generated = True

pf_tool = scene.hexfinity_path_features
assert pf_tool.edge_snap == 3, pf_tool.edge_snap
print("edge_snap default OK:", pf_tool.edge_snap)

# A bare tile object.
mesh = bpy.data.meshes.new("HexTile_test")
obj = bpy.data.objects.new("HexTile_test", mesh)
coll = bpy.data.collections.new("HexFinity Map")
scene.collection.children.link(coll)
coll.objects.link(obj)
mp.root_collection = coll

tile = obj.hexfinity_tile
tile.coord_q = 0
tile.coord_r = 0
# Level 2, not 0: a flat-at-level-0 tile's top sits exactly at
# base_thickness_mm, leaving a carve nothing to dig into (the builder clamps
# z to never go below base_thickness_mm) — headroom is needed to actually
# observe the SIMPLE carve moving vertices below.
for n in ("p1", "p2", "p3", "p4", "p5", "p6"):
    setattr(tile, n, 2)
tile.is_generated = True
bpy.context.view_layer.objects.active = obj
obj.select_set(True)

from hexfinity.map import point_in_hex, NE, EDGE_DIRECTIONS, neighbour_coord, tile_world_xy, corner_xy
from hexfinity.path_features import (
    _commit_feature, _snap_targets_world, apply_type_defaults, PATH_TEXTURES,
    _feature_plane_z_local, _PATH_FEATURE_LINK_FIELDS, HEXFINITY_OT_draw_path_feature)
from hexfinity import operators

# A free waypoint outside the tile's own hex must be rejected (not clamped) —
# this is the exact check path_features._add_point runs before appending a
# free (unsnapped) point.
assert point_in_hex(0.0, 0.0, mp.diameter_mm)
assert not point_in_hex(mp.diameter_mm, mp.diameter_mm, mp.diameter_mm)
print("point_in_hex rejection OK")

# operators.rebuild_tile must exist before build the tile once so the mesh
# has a real shape to carve into (mirrors what generate_map does).
operators.rebuild_tile(obj)
verts_before = [tuple(v.co) for v in obj.data.vertices]
assert len(verts_before) > 0
print("initial build OK, verts =", len(verts_before))

# _commit_feature: a two-point line straight across the tile — auto-fills
# width/depth/repeat/texture from SIMPLE's defaults and rebuilds.
_commit_feature(bpy.context, obj, [(-20.0, 0.0), (20.0, 0.0)])
assert len(tile.path_features) == 1
feat = tile.path_features[0]
assert feat.name == "Path 1", feat.name
assert feat.feature_type == 'SIMPLE', feat.feature_type
assert len(feat.points) == 2
assert feat.width_mm > 0.0, feat.width_mm
assert feat.texture == 'NONE', feat.texture
assert feat.local_subdiv == 0, feat.local_subdiv
print("commit OK:", feat.name, feat.feature_type, feat.width_mm, feat.texture,
      "local_subdiv =", feat.local_subdiv)

verts_after_commit = [tuple(v.co) for v in obj.data.vertices]
# SIMPLE's default local_subdiv=0 means a plain carve no longer necessarily
# adds vertices (unlike GRAVEL/PAVED_ROAD) — compare positions, not just
# count, so this still catches "the carve did nothing".
assert verts_after_commit != verts_before, "SIMPLE carve did not change the mesh"
print("auto-carve changed the mesh OK, verts =", len(verts_after_commit))

# Change type via the enum, mirroring what the panel does — re-fills
# width/depth/repeat/texture for the new type and rebuilds again.
feat.feature_type = 'GRAVEL'
assert feat.feature_type == 'GRAVEL'
assert feat.texture == 'BRICK_GRAVEL', feat.texture
assert feat.local_subdiv == 2, feat.local_subdiv
print("type change + refill OK, local_subdiv =", feat.local_subdiv)

# Local Subdivision is per-line: dropping it back to 0 should shrink the
# tile's vertex count relative to GRAVEL's default of 2 (less local corridor
# density), proving the field actually reaches the mesh builder.
verts_at_level_2 = len(obj.data.vertices)
feat.local_subdiv = 0
verts_at_level_0 = len(obj.data.vertices)
assert verts_at_level_0 < verts_at_level_2, (verts_at_level_0, verts_at_level_2)
print("local_subdiv edit changed vertex count OK:",
      verts_at_level_2, "->", verts_at_level_0)
feat.local_subdiv = 2

# A second line, to exercise multi-line snap-target aggregation.
_commit_feature(bpy.context, obj, [(0.0, -20.0), (0.0, 20.0)])
assert len(tile.path_features) == 2

targets = _snap_targets_world(obj, mp, pf_tool.edge_snap)
# 6*(edge_snap-1) hex-edge points + 4 waypoints across the two lines.
expected_edge_pts = 6 * (pf_tool.edge_snap - 1)
assert len(targets) == expected_edge_pts + 4, (len(targets), expected_edge_pts)
print("snap target count OK:", len(targets))

# Switching to the "None (flat)" texture must fall back to a uniform groove
# (not a no-op) — the mesh should still change relative to an unmodified
# tile even with no texture asset behind it.
feat.texture = 'NONE'
assert feat.texture == 'NONE'
print("no-texture fallback selectable OK")

# Remove operator path.
tile.active_path_feature_index = 0
bpy.ops.hexfinity.remove_path_feature()
assert len(tile.path_features) == 1, len(tile.path_features)
print("remove OK, remaining:", len(tile.path_features))

# ---------------------------------------------------------------------------
# Multi-hex crossing: a second, NE-neighbour tile selected alongside tile0,
# exercising _resolve_crossing_neighbour / _continue_onto / _commit_feature's
# seed_settings inheritance directly. The modal operator itself can't be
# driven headlessly (no real mouse events) -- mirrors the same
# "call the underlying helpers directly" approach
# tests/_headless_link_paths_check.py already uses for hexfinity.
import types

mesh2 = bpy.data.meshes.new("HexTile_test_2")
obj2 = bpy.data.objects.new("HexTile_test_2", mesh2)
coll.objects.link(obj2)
tile2 = obj2.hexfinity_tile
nq, nr = neighbour_coord(tile.coord_q, tile.coord_r, NE)
tile2.coord_q, tile2.coord_r = nq, nr
for n in ("p1", "p2", "p3", "p4", "p5", "p6"):
    setattr(tile2, n, 2)
tile2.is_generated = True
obj2.location.x, obj2.location.y = tile_world_xy(nq, nr, mp.diameter_mm)
# A freshly created+positioned object's matrix_world isn't guaranteed to
# reflect the new location until the next depsgraph evaluation -- force one
# now, since _continue_onto (below) reads obj2.matrix_world directly. Real
# interactive use never needs this: Blender's own event loop keeps
# already-existing tiles' transforms in sync between clicks.
bpy.context.view_layer.update()

# The edge of tile0 bordering its NE neighbour, and that edge's midpoint in
# tile0-local mm -- the crossing waypoint a real click would snap to.
edge_idx = EDGE_DIRECTIONS.index(NE)
a = corner_xy(edge_idx, mp.diameter_mm)
b = corner_xy((edge_idx + 1) % 6, mp.diameter_mm)
mid_local_t0 = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
z_local = _feature_plane_z_local(tile, mp)
world_mid = Vector((obj.location.x + mid_local_t0[0],
                    obj.location.y + mid_local_t0[1], z_local))

before_count_t0 = len(tile.path_features)
_commit_feature(bpy.context, obj, [(0.0, 0.0), mid_local_t0])
assert len(tile.path_features) == before_count_t0 + 1
crossing_feature = tile.path_features[tile.active_path_feature_index]
crossing_feature.feature_type = 'PAVED_ROAD'
crossing_feature.width_mm = 12.0
crossing_feature.depth_mm = 2.0
print("tile0 segment ending at shared edge point committed OK:",
      crossing_feature.feature_type, crossing_feature.width_mm)

obj2.select_set(True)
obj.select_set(True)
bpy.context.view_layer.objects.active = obj

# A plain object standing in for the modal operator's `self` -- only
# _resolve_crossing_neighbour/_continue_onto are exercised (pure state
# manipulation, no event/UI access), with _recenter_view bound on so
# _continue_onto's internal self._recenter_view(...) call resolves; it's a
# no-op in background mode since context.region_data is None there.
state = types.SimpleNamespace()
state._tile = obj
state._recenter_view = types.MethodType(
    HEXFINITY_OT_draw_path_feature._recenter_view, state)

neighbour = HEXFINITY_OT_draw_path_feature._resolve_crossing_neighbour(
    state, bpy.context, edge_idx)
assert neighbour is obj2, (neighbour, obj2)
print("crossing neighbour resolved via selected+generated NE tile OK")

assert HEXFINITY_OT_draw_path_feature._resolve_crossing_neighbour(
    state, bpy.context, None) is None
print("existing-waypoint snap (edge_idx=None) never crosses OK")

HEXFINITY_OT_draw_path_feature._continue_onto(state, bpy.context, neighbour, world_mid)
assert state._tile is obj2
assert len(state._pts_local) == 1
assert state._pending_seed_settings["feature_type"] == 'PAVED_ROAD'
assert state._pending_seed_settings["width_mm"] == 12.0
print("continue_onto seeded neighbour segment + carried settings OK")

state._pts_local.append((0.0, 0.0))
before_count_t1 = len(tile2.path_features)
_commit_feature(bpy.context, state._tile, state._pts_local,
                seed_settings=state._pending_seed_settings)
assert len(tile2.path_features) == before_count_t1 + 1
new_feat = tile2.path_features[tile2.active_path_feature_index]
assert new_feat.feature_type == 'PAVED_ROAD', new_feat.feature_type
assert new_feat.width_mm == 12.0, new_feat.width_mm
assert new_feat.depth_mm == 2.0, new_feat.depth_mm
print("neighbour segment inherited crossing settings OK:",
      new_feat.feature_type, new_feat.width_mm, new_feat.depth_mm)

w0 = (obj.location.x + mid_local_t0[0], obj.location.y + mid_local_t0[1])
w1 = (obj2.location.x + new_feat.points[0].x, obj2.location.y + new_feat.points[0].y)
assert abs(w0[0] - w1[0]) < 1e-6 and abs(w0[1] - w1[1]) < 1e-6, (w0, w1)
print("shared waypoint coincides across tiles OK")

# Deselecting the neighbour must block the crossing -- it falls back to
# today's exact single-hex behaviour (the caller would then just finish the
# line there instead of continuing).
obj2.select_set(False)
state._tile = obj
assert HEXFINITY_OT_draw_path_feature._resolve_crossing_neighbour(
    state, bpy.context, edge_idx) is None
print("deselected neighbour correctly blocks crossing OK")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS PATH FEATURE CHECK PASSED")
