"""Headless smoke test: register the extension and exercise the River path
feature end to end in real bpy, including the Ocean-modifier ripple bake
(the one piece of this feature that can't be covered by the bpy-free pytest
suite, since bpy.types.OceanModifier is a real mesh modifier). Run with:
    blender --background --python tests/_headless_river_check.py
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
    setattr(tile, n, 3)
tile.is_generated = True
bpy.context.view_layer.objects.active = obj
obj.select_set(True)

from hexfinity import operators
from hexfinity import path_features as pf

operators.rebuild_tile(obj)
verts_before = [tuple(v.co) for v in obj.data.vertices]
assert len(verts_before) > 0
print("initial build OK, verts =", len(verts_before))

# Add a River feature directly (mirrors _commit_feature, but sets the type
# to RIVER instead of leaving the SIMPLE default).
feature = tile.path_features.add()
for (x, y) in [(-30.0, 0.0), (30.0, 0.0)]:
    p = feature.points.add()
    p.x, p.y = x, y
tile.active_path_feature_index = len(tile.path_features) - 1
feature.feature_type = 'RIVER'

expected_width = 3.0 * mp.man_height_mm
assert abs(feature.width_mm - expected_width) < 1e-6, (feature.width_mm, expected_width)
assert feature.depth_levels == 1, feature.depth_levels
assert feature.embankment_angle_deg == 45.0, feature.embankment_angle_deg
assert feature.embankment_variation_mm > 0.0, feature.embankment_variation_mm
assert feature.river_bottom_style == 'NONE', feature.river_bottom_style
assert feature.local_subdiv == 3, feature.local_subdiv
print("RIVER type defaults OK: width =", feature.width_mm,
      "depth_levels =", feature.depth_levels,
      "angle =", feature.embankment_angle_deg,
      "variation =", feature.embankment_variation_mm)

verts_after_river = [tuple(v.co) for v in obj.data.vertices]
assert verts_after_river != verts_before, "RIVER carve did not change the mesh"
print("RIVER auto-carve changed the mesh OK, verts =", len(verts_after_river))

specs = pf.path_specs(obj)
river_specs = [s for s in specs if s.get("kind") == "river"]
assert len(river_specs) == 1, specs
spec = river_specs[0]
assert abs(spec["depth_mm"] - mp.level_height_mm) < 1e-6, spec["depth_mm"]
assert "pixels" not in spec, "Flat bottom style should not bake an Ocean heightfield"
print("path_specs() Flat-style dict OK:", {k: v for k, v in spec.items()
      if k not in ("points",)})

# Now switch the Bottom style to Tessendorf's FFT — this is the risky,
# bpy-only path: a scratch object with a real Ocean modifier gets created,
# evaluated through the depsgraph, and torn down.
n_objects_before = len(bpy.data.objects)
n_meshes_before = len(bpy.data.meshes)

feature.river_bottom_style = 'TESSENDORF_FFT'
assert feature.river_bottom_style == 'TESSENDORF_FFT'

specs2 = pf.path_specs(obj)
river_specs2 = [s for s in specs2 if s.get("kind") == "river"]
assert len(river_specs2) == 1
spec2 = river_specs2[0]
assert spec2.get("pixels"), "expected a baked Ocean heightfield pixel grid"
w, h = spec2["tex_width"], spec2["tex_height"]
assert w > 0 and h > 0, (w, h)
assert len(spec2["pixels"]) == w * h, (len(spec2["pixels"]), w, h)
assert all(0.0 <= px <= 1.0 for px in spec2["pixels"]), "pixels must be normalized to [0,1]"
print("Ocean heightfield bake OK: grid =", w, "x", h,
      "min =", min(spec2["pixels"]), "max =", max(spec2["pixels"]))

# The scratch object/mesh must not leak into the .blend's datablocks.
n_objects_after = len(bpy.data.objects)
n_meshes_after = len(bpy.data.meshes)
assert n_objects_after == n_objects_before, (n_objects_before, n_objects_after)
assert n_meshes_after == n_meshes_before, (n_meshes_before, n_meshes_after)
print("no leaked scratch objects/meshes OK")

# Calling again with the same seed/patch must hit the session cache and
# return the identical grid (not just an equal one — same cached object).
spec3 = [s for s in pf.path_specs(obj) if s.get("kind") == "river"][0]
assert spec3["pixels"] is spec2["pixels"], "expected the session cache to be reused"
print("Ocean heightfield cache reuse OK")

# Rebuild the tile itself with the rippled bottom active — must not raise
# and must still produce a manifold-checked mesh (build_hex_tile asserts
# this internally).
operators.rebuild_tile(obj)
verts_rippled = [tuple(v.co) for v in obj.data.vertices]
assert verts_rippled != verts_after_river, "ripple did not change the mesh"
print("full rebuild with Tessendorf's FFT bottom OK, verts =", len(verts_rippled))

feature.river_bottom_style = 'NONE'
operators.rebuild_tile(obj)
print("switched back to Flat OK")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS RIVER CHECK PASSED")
