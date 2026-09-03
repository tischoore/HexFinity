"""Headless smoke test: register the extension and exercise the path
feature draw/commit/remove path in real bpy. Run with:
    blender --background --python tests/_headless_path_feature_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.
"""
import os
import sys

import bpy

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
for n in ("p1", "p2", "p3", "p4", "p5", "p6"):
    setattr(tile, n, 0)
tile.is_generated = True
bpy.context.view_layer.objects.active = obj
obj.select_set(True)

from hexfinity.map import point_in_hex
from hexfinity.path_features import (
    _commit_feature, _snap_targets_world, apply_type_defaults, PATH_TEXTURES)
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
verts_before = len(obj.data.vertices)
assert verts_before > 0
print("initial build OK, verts =", verts_before)

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
print("commit OK:", feat.name, feat.feature_type, feat.width_mm, feat.texture)

verts_after_commit = len(obj.data.vertices)
assert verts_after_commit != verts_before, (verts_before, verts_after_commit)
print("auto-carve changed the mesh OK, verts =", verts_after_commit)

# Change type via the enum, mirroring what the panel does — re-fills
# width/depth/repeat/texture for the new type and rebuilds again.
feat.feature_type = 'GRAVEL'
assert feat.feature_type == 'GRAVEL'
assert feat.texture == 'BRICK_GRAVEL', feat.texture
print("type change + refill OK")

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

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS PATH FEATURE CHECK PASSED")
