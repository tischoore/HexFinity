"""Headless smoke test: register the extension, generate a small map, build
path features directly via the property API (mirroring
path_features._commit_feature's own sequence, since the modal draw tool
can't be scripted headlessly), then exercise hexfinity.link_connected_paths
-- the operator the "Link Connected Paths" button invokes -- and assert
settings propagate transitively across a cross-tile shared waypoint, a
same-tile fork, and a 3-way cycle without hanging or crashing. Run with:
    blender --background --python tests/_headless_link_paths_check.py
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
mp.grid_x = 2
mp.grid_y = 1

res = bpy.ops.hexfinity.generate_map()
assert res == {'FINISHED'}, res


def _tile_at(q, r):
    for o in mp.root_collection.objects:
        tp = o.hexfinity_tile
        if tp.is_generated and tp.coord_q == q and tp.coord_r == r:
            return o
    return None


tile0 = _tile_at(0, 0)
tile1 = _tile_at(1, 0)
assert tile0 is not None and tile1 is not None
print("generate OK; tile0 =", tile0.name, "tile1 =", tile1.name)


def _commit(tile_obj, points_local, feature_type='SIMPLE'):
    """Mirrors path_features._commit_feature: add points, then set
    feature_type last so its autofill/rebuild callback fires once."""
    tile = tile_obj.hexfinity_tile
    feature = tile.path_features.add()
    for (x, y) in points_local:
        p = feature.points.add()
        p.x, p.y = x, y
    tile.active_path_feature_index = len(tile.path_features) - 1
    feature.feature_type = feature_type
    return feature


# A genuine shared rim point on the seam between tile0 (q=0,r=0) and tile1
# (q=1,r=0): tile1 is tile0's NE neighbour, and per map.SHARED_CORNERS,
# tile0's P1 (corner index 0) coincides with tile1's P5 (corner index 4).
from hexfinity.map import corner_xy

shared_local_t0 = corner_xy(0, mp.diameter_mm)   # tile0's P1
shared_local_t1 = corner_xy(4, mp.diameter_mm)   # tile1's P5
_w0 = (tile0.location.x + shared_local_t0[0], tile0.location.y + shared_local_t0[1])
_w1 = (tile1.location.x + shared_local_t1[0], tile1.location.y + shared_local_t1[1])
assert abs(_w0[0] - _w1[0]) < 1e-6 and abs(_w0[1] - _w1[1]) < 1e-6, (
    "tile0.P1 and tile1.P5 should coincide in world space", _w0, _w1)

# Feature A: tile0, from its centre out to the shared seam point.
feat_a = _commit(tile0, [(0.0, 0.0), shared_local_t0], feature_type='SIMPLE')
feat_a.width_mm = 3.0
feat_a.depth_mm = 0.15

# Feature B: tile1, from the same shared seam point (in tile1's local frame)
# out to its own centre -- should be found as connected to A via the shared
# world point.
feat_b = _commit(tile1, [shared_local_t1, (0.0, 0.0)], feature_type='SIMPLE')
assert feat_b.width_mm != 9.0 and feat_b.depth_mm != 1.5, "sanity: not already matching"

# Feature C: a same-tile fork off an INTERIOR waypoint of A (A's own centre
# point (0,0) on tile0) -- should also be found as connected.
feat_c = _commit(tile0, [(0.0, 0.0), (-40.0, 20.0)], feature_type='SIMPLE')

# Feature D: a second-generation fork, off B's own (0,0) waypoint on tile1
# (B is itself only reachable from A via the cross-tile seam point) -- checks
# that propagation is transitive (A -> B -> D), not just one hop.
# (The BFS's cycle-safety itself -- A/B/C mutually reachable in a loop -- is
# covered directly at the map.find_connected_component level in
# tests/test_map.py, since that's the pure algorithm the cycle guarantee
# actually lives in.)
feat_d = _commit(tile1, [(0.0, 0.0), (10.0, 10.0)], feature_type='SIMPLE')

print("committed 4 path features: A(tile0) -[seam]- B(tile1) -[fork]- D(tile1); "
      "C(tile0) forks off A's own centre point")

# ---- Link from A: set distinctive settings on A, then link -------------
feat_a.feature_type = 'PAVED_ROAD'
feat_a.width_mm = 9.0
feat_a.depth_mm = 1.5
feat_a.repeat_mm = 12.0
feat_a.local_subdiv = 2
a_name_before = feat_a.name
b_name_before = feat_b.name

tile0.hexfinity_tile.active_path_feature_index = list(tile0.hexfinity_tile.path_features).index(feat_a)
bpy.context.view_layer.objects.active = tile0
res = bpy.ops.hexfinity.link_connected_paths()
assert res == {'FINISHED'}, res

for label, f, obj in (("B", feat_b, tile1), ("C", feat_c, tile0), ("D", feat_d, tile1)):
    assert f.feature_type == 'PAVED_ROAD', (label, f.feature_type)
    assert f.width_mm == 9.0, (label, f.width_mm)
    assert f.depth_mm == 1.5, (label, f.depth_mm)
    assert f.repeat_mm == 12.0, (label, f.repeat_mm)
    assert f.local_subdiv == 2, (label, f.local_subdiv)

assert feat_a.name == a_name_before, "source name should be untouched"
assert feat_b.name == b_name_before, "connected path's own name must NOT be overwritten"
assert len(feat_a.points) == 2 and len(feat_b.points) == 2, "waypoints must be untouched"

print("link_connected_paths OK: B/C/D all picked up A's settings; names/points untouched")

# ---- No-op case: an isolated feature with no shared waypoint -----------
isolated = _commit(tile1, [(50.0, -50.0), (60.0, -60.0)], feature_type='SIMPLE')
tile1.hexfinity_tile.active_path_feature_index = list(tile1.hexfinity_tile.path_features).index(isolated)
bpy.context.view_layer.objects.active = tile1
res = bpy.ops.hexfinity.link_connected_paths()
assert res == {'CANCELLED'}, res
print("isolated feature correctly reports nothing to link")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS LINK-PATHS CHECK PASSED")
