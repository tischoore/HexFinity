"""Headless smoke test: register the extension and exercise the terrain
feature draw/commit/remove path in real bpy. Run with:
    blender --background --python tests/_headless_terrain_feature_check.py
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

tf_tool = scene.hexfinity_terrain_features
assert tf_tool.edge_snap == 3, tf_tool.edge_snap
print("edge_snap default OK:", tf_tool.edge_snap)

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
from hexfinity.terrain_features import (
    _commit_feature, _snap_targets_world, HEXFINITY_OT_generate_terrain_features)

# A free waypoint outside the tile's own hex must be rejected (not clamped) —
# this is the exact check terrain_features._add_point runs before appending a
# free (unsnapped) point.
assert point_in_hex(0.0, 0.0, mp.diameter_mm)
assert not point_in_hex(mp.diameter_mm, mp.diameter_mm, mp.diameter_mm)
print("point_in_hex rejection OK")

# _commit_feature: a two-point line.
_commit_feature(obj, [(-20.0, 0.0), (20.0, 0.0)])
assert len(tile.terrain_features) == 1
feat = tile.terrain_features[0]
assert feat.name == "Feature 1", feat.name
assert feat.feature_type == 'FOOTPATH', feat.feature_type
assert len(feat.points) == 2
print("commit OK:", feat.name, feat.feature_type, len(feat.points))

# Change type via the enum, mirroring what the panel does.
feat.feature_type = 'GRAVEL_ROAD'
assert feat.feature_type == 'GRAVEL_ROAD'
print("type change OK")

# A second line, to exercise multi-line snap-target aggregation.
_commit_feature(obj, [(0.0, -20.0), (0.0, 20.0)])
assert len(tile.terrain_features) == 2

targets = _snap_targets_world(obj, mp, tf_tool.edge_snap)
# 6*(edge_snap-1) hex-edge points + 4 waypoints across the two lines.
expected_edge_pts = 6 * (tf_tool.edge_snap - 1)
assert len(targets) == expected_edge_pts + 4, (len(targets), expected_edge_pts)
print("snap target count OK:", len(targets))

# Remove operator path.
tile.active_terrain_feature_index = 0
bpy.ops.hexfinity.remove_terrain_feature()
assert len(tile.terrain_features) == 1, len(tile.terrain_features)
print("remove OK, remaining:", len(tile.terrain_features))

# Generate must stay disabled (poll() always False).
assert not HEXFINITY_OT_generate_terrain_features.poll(bpy.context)
print("generate poll() False OK")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS TERRAIN FEATURE CHECK PASSED")
