"""Headless smoke test: register the extension and exercise the Draw Area
"Flood Fill" authoring path in real bpy. Run with:
    blender --background --python tests/_headless_flood_fill_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.

The modal operator's own raycast needs a real viewport, so this drives the
same underlying pieces directly: `face_select`'s pure geometry against the
real rebuilt tile mesh, then `regions._commit_region` — the identical
finalize path `HEXFINITY_OT_flood_fill_region`/`HEXFINITY_OT_draw_region`
both call.
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
    setattr(tile, n, 0)
tile.is_generated = True

from hexfinity import operators, face_select, regions

operators.rebuild_tile(obj)
assert len(tile.surface_regions) == 0
print("baseline rebuild OK; verts =", len(obj.data.vertices))

verts = [tuple(v.co) for v in obj.data.vertices]
faces = [tuple(p.vertices) for p in obj.data.polygons]

# On a flat tile every top face is exactly coplanar, so a small angle
# tolerance from any top-face seed reaches a sizeable connected patch.
# Face 0 is a top face -- build_hex_tile emits top faces first.
selected = face_select.flood_fill_faces(verts, faces, seed_index=0, angle_threshold_deg=5.0)
assert len(selected) > 1, selected
print("flood_fill_faces selected", len(selected), "faces")

loop = face_select.boundary_loop(faces, selected)
assert loop is not None and len(loop) >= 3, loop
print("boundary_loop:", len(loop), "vertices")

pts_local = face_select.loop_to_xy(verts, loop)
regions._commit_region(obj, pts_local)
assert len(tile.surface_regions) == 1
reg = tile.surface_regions[0]
assert len(reg.points) == len(pts_local)
assert reg.surface_type != "NONE"
print("committed region: surface_type =", reg.surface_type,
      "points =", len(reg.points), "verts =", len(obj.data.vertices))

# A whole-mesh selection (very generous threshold) must still resolve to
# the tile's own single outer boundary loop -- no pinch points/islands.
full_selected = face_select.flood_fill_faces(verts, faces, seed_index=0, angle_threshold_deg=89.0)
full_loop = face_select.boundary_loop(faces, full_selected)
assert full_loop is not None
print("full-tile flood fill boundary loop OK:", len(full_loop), "vertices")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS FLOOD FILL CHECK PASSED")
