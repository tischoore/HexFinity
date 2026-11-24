"""Headless smoke test for the Flagstone Path Feature: stones must sit ON the
surface (never with terrain poking up through a slab), the corridor bed is
flattened, and the 'Flatten Bed' toggle works. Run with:
    blender --background --python tests/_headless_flagstone_path_check.py
Exits non-zero on failure (raises).
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

scene = bpy.context.scene
mp = scene.hexfinity_map
mp.diameter_mm = 100.0
mp.level_height_mm = 4.0
mp.base_thickness_mm = 10.0
mp.smoothness_passes = 2
mp.resample_density = 0
mp.man_height_mm = 10.0
mp.is_generated = True

mesh = bpy.data.meshes.new("HexTile_test")
obj = bpy.data.objects.new("HexTile_test", mesh)
coll = bpy.data.collections.new("HexFinity Map")
scene.collection.children.link(coll)
coll.objects.link(obj)
mp.root_collection = coll

tile = obj.hexfinity_tile
tile.coord_q = 0
tile.coord_r = 0
for n, lv in zip(("p1", "p2", "p3", "p4", "p5", "p6"), (0, 1, 3, 4, 3, 1)):
    setattr(tile, n, lv)
tile.override_center = True
tile.center_level = 5            # a hill the path crosses
tile.is_generated = True
bpy.context.view_layer.objects.active = obj
obj.select_set(True)

from hexfinity import operators, flagstones

operators.rebuild_tile(obj)

feature = tile.path_features.add()
for (x, y) in [(-40.0, -5.0), (0.0, 3.0), (40.0, -2.0)]:
    p = feature.points.add()
    p.x, p.y = x, y
feature.feature_type = 'FLAGSTONE'   # autofill + rebuild
print("thickness_mm =", round(feature.thickness_mm, 4),
      "local_subdiv =", feature.local_subdiv, "flatten_bed =", feature.flatten_bed)
assert feature.thickness_mm < 1.0, "thickness still clamped up"
assert feature.flatten_bed


def flag_obj():
    objs = [c for c in obj.children if c.get(flagstones.FLAGSTONE_OF)]
    assert len(objs) == 1, objs
    return objs[0]


def worst_penetration():
    """Max (terrain z - slab bottom z) over a dense sample of every stone's
    bottom face, via fresh raycasts onto the tile."""
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    fo = flag_obj()
    me = fo.data
    worst = -1e9
    for poly in me.polygons:
        if poly.normal.z > -0.5:     # bottom faces only
            continue
        ring = [me.vertices[i].co.copy() for i in poly.vertices]
        c = sum(ring, Vector()) / len(ring)
        for v in ring:
            for t in (0.0, 0.2, 0.4, 0.6, 0.8, 0.95):
                q = c + (v - c) * t
                hit, loc, nrm, _ = obj.ray_cast(Vector((q.x, q.y, 1e5)),
                                                Vector((0, 0, -1)), depsgraph=dg)
                if hit and nrm.z > 0.3:
                    worst = max(worst, loc.z - q.z)
    return worst, len(me.polygons)


sink = min(flagstones.SINK_MM_MAX, feature.thickness_mm * flagstones.SINK_FRACTION)
w_bed, nfaces = worst_penetration()
print(f"flatten_bed ON : worst penetration {w_bed:.4f} mm (sink {sink:.4f}), faces {nfaces}")
assert w_bed <= sink + 0.03, w_bed

feature.flatten_bed = False
w_raw, _ = worst_penetration()
print(f"flatten_bed OFF: worst penetration {w_raw:.4f} mm")
assert w_raw <= sink + 0.08, w_raw

print("FLAGSTONE PATH CHECK OK")
