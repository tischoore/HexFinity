"""Headless smoke test: register the extension and exercise the region rebuild
path in real bpy. Run with:
    blender --background --python tests/_headless_region_check.py
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
mp.smoothness_passes = 3      # enough resolution for a small feature
mp.resample_density = 0
mp.man_height_mm = 10.0
mp.is_generated = True

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

# man-height-derived default check.
from hexfinity import procedural_surfaces as ps
exp = ps.feature_mm_default("COBBLESTONE", mp.man_height_mm)
print(f"feature_mm_default(COBBLESTONE,{mp.man_height_mm}) =", exp)

# Add a region with a drawn loop near the centre, then a whole-tile furrow.
region = tile.surface_regions.add()
for (x, y) in [(-30, -30), (30, -30), (30, 30), (-30, 30)]:
    p = region.points.add()
    p.x, p.y = float(x), float(y)
region.surface_type = "COBBLESTONE"   # fires auto-fill + rebuild
assert abs(region.feature_mm - exp) < 1e-6, region.feature_mm
print("region 1 (cobblestone loop): feature_mm =", region.feature_mm,
      "depth =", region.depth_mm, "verts =", len(obj.data.vertices))

r2 = tile.surface_regions.add()
r2.surface_type = "FURROW"            # whole-tile, anisotropic
r2.direction_deg = 30.0

from hexfinity import operators
operators.rebuild_tile(obj)
nverts = len(obj.data.vertices)
nfaces = len(obj.data.polygons)
assert nverts > 0 and nfaces > 0, (nverts, nfaces)
print("after 2 regions: verts =", nverts, "faces =", nfaces)

# Toggle man-height and confirm the global rebuild path runs.
mp.man_height_mm = 56.0
print("man_height change OK; verts =", len(obj.data.vertices))

# ---- Surface Texture (whole-tile base layer, no drawing) check ------------
baseline_verts = [tuple(v.co) for v in obj.data.vertices]

tile.surface_texture.surface_type = "PLAINS"   # fires auto-fill + rebuild
operators.rebuild_tile(obj)
textured_verts = [tuple(v.co) for v in obj.data.vertices]
assert len(textured_verts) == len(baseline_verts)
assert any(a != b for a, b in zip(baseline_verts, textured_verts)), \
    "Surface Texture PLAINS should move top vertices"
print("surface_texture PLAINS: verts =", len(obj.data.vertices),
      "faces =", len(obj.data.polygons))

# Rim corners must stay flat despite the whole-tile texture (mesh_builder's
# rim-fade damps every region -- including this singleton -- to zero at the
# hex boundary).
import math as _math
R = mp.diameter_mm * 0.5
for a, b in zip(baseline_verts, textured_verts):
    if abs(_math.hypot(a[0], a[1]) - R) < 1e-3:
        assert abs(a[2] - b[2]) < 1e-6, (a, b)
print("surface_texture PLAINS: rim stays flat")

tile.surface_texture.surface_type = "NONE"
operators.rebuild_tile(obj)
reverted_verts = [tuple(v.co) for v in obj.data.vertices]
assert reverted_verts == baseline_verts, "toggling back to NONE must be a no-op"
print("surface_texture toggle-off reverts exactly")

# ---- Surface Texture Copy/Apply check --------------------------------------
bpy.context.view_layer.objects.active = obj
obj.select_set(True)

try:
    bpy.ops.hexfinity.apply_surface_texture()
    raise AssertionError("apply_surface_texture should be disabled before any copy")
except RuntimeError:
    print("apply_surface_texture correctly disabled before any copy")

reg = tile.surface_texture
reg.surface_type = "COBBLESTONE"      # fires auto-fill first...
reg.feature_mm = 3.5                  # ...then these overwrite it with a
reg.depth_mm = 4.5                    # distinct custom combo, so the
reg.regularity = 0.75                 # clipboard captures what was actually
reg.direction_deg = 45.0              # set, not the auto-filled defaults.
reg.mask_falloff_mm = 6.0
reg.param0 = 0.1
reg.param1 = 0.2
reg.param2 = 0.3
reg.param3 = 0.4
reg.scatter_merge = True
operators.rebuild_tile(obj)

bpy.ops.hexfinity.copy_surface_texture()
clip = dict(operators._SURFACE_TEXTURE_CLIPBOARD)
assert clip is not None

# A second bare tile with a different starting surface_type, so Apply's
# type-defaulting-then-overwrite path (see HEXFINITY_OT_apply_surface_texture)
# is actually exercised, not a same-type no-op.
mesh2 = bpy.data.meshes.new("HexTile_test2")
obj2 = bpy.data.objects.new("HexTile_test2", mesh2)
coll.objects.link(obj2)
tile2 = obj2.hexfinity_tile
tile2.coord_q = 1
tile2.coord_r = 0
for n in ("p1", "p2", "p3", "p4", "p5", "p6"):
    setattr(tile2, n, 0)
tile2.is_generated = True
tile2.surface_texture.surface_type = "FURROW"
operators.rebuild_tile(obj2)

for o in bpy.context.selected_objects:
    o.select_set(False)
obj.select_set(True)
obj2.select_set(True)
bpy.context.view_layer.objects.active = obj

bpy.ops.hexfinity.apply_surface_texture()

for target, label in ((obj, "active"), (obj2, "fanned-out")):
    r = target.hexfinity_tile.surface_texture
    for f in operators._SURFACE_TEXTURE_FIELDS:
        got, want = getattr(r, f), clip[f]
        assert got == want, (label, f, got, want)
print("surface_texture copy/apply: both tiles match clipboard")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS REGION CHECK PASSED")
