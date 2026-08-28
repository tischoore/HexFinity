"""Headless smoke test: register the extension, generate a sloped single tile,
plant a tree, and exercise the tree-base-pad flatten path plus its property
update callbacks. Run with:
    blender --background --python tests/_headless_flora_pad_check.py
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
mp.smoothness_passes = 3
mp.resample_density = 0
mp.man_height_mm = 10.0
mp.grid_x = 0
mp.grid_y = 0

res = bpy.ops.hexfinity.generate_map()
assert res == {'FINISHED'}, res
tile = next(o for o in mp.root_collection.objects if o.hexfinity_tile.is_generated)
tprops = tile.hexfinity_tile

# A real slope, so a flat pad has something to flatten against.
for n, level in zip(("p1", "p2", "p3", "p4", "p5", "p6"), (0, 1, 2, 3, 4, 5)):
    setattr(tprops, n, level)

baseline_verts = len(tile.data.vertices)
print("sloped tile baseline verts:", baseline_verts)

# ---- Plant a tree near the tile centre (bypasses the modal picker — this is
# exactly the state HEXFINITY_OT_flora_marker._place_tree writes). ----------
placement = tprops.flora_placements.add()
placement.species_file = "LeafyTree_Small_1.stl"
placement.tree_type = 'LEAFY_TREE'
placement.local_x_mm = 5.0
placement.local_y_mm = -5.0
placement.rotation_rad = 0.3
placement.scale_factor = 1.0

from hexfinity import operators
operators.rebuild_tile(tile)

padded_verts = len(tile.data.vertices)
assert padded_verts > baseline_verts, (
    "planting a tree on a slope with flatten_base on should refine+add verts",
    baseline_verts, padded_verts)
print("after planting on slope: verts =", padded_verts, "(+%d)" % (padded_verts - baseline_verts))

tree_objs = [c for c in tile.children if c.get("hf_flora_of")]
assert len(tree_objs) == 1, tree_objs
print("planted tree object:", tree_objs[0].name, "at", tuple(tree_objs[0].location))

# ---- flatten_base toggle: off should drop the pad's extra verts. ----------
flora_props = scene.hexfinity_flora
old_data = tile.data
flora_props.flatten_base = False
assert tile.data is not old_data, "toggling flatten_base should trigger a rebuild"
off_verts = len(tile.data.vertices)
assert off_verts == baseline_verts, (
    "flatten_base off should match the unpadded vertex count", off_verts, baseline_verts)
print("flatten_base off: verts =", off_verts, "(back to baseline)")

old_data = tile.data
flora_props.flatten_base = True
assert tile.data is not old_data, "re-enabling flatten_base should trigger a rebuild"
on_verts = len(tile.data.vertices)
assert on_verts == padded_verts, (on_verts, padded_verts)
print("flatten_base back on: verts =", on_verts)

# ---- pad_blend_mm / penetration_mm: dragging either re-seats live. --------
old_data = tile.data
flora_props.pad_blend_mm = 8.0
assert tile.data is not old_data, "pad_blend_mm should trigger a rebuild"
print("pad_blend_mm change triggered rebuild OK")

old_z = [c for c in tile.children if c.get("hf_flora_of")][0].location.z
old_data = tile.data
flora_props.penetration_mm = 3.0
assert tile.data is not old_data, "penetration_mm should trigger a rebuild"
new_tree = [c for c in tile.children if c.get("hf_flora_of")][0]
assert new_tree.location.z != old_z, "penetration_mm should visibly re-seat the tree"
print("penetration_mm change re-seated the tree OK (z: %.4f -> %.4f)" % (old_z, new_tree.location.z))

# ---- Export must still see a valid manifold tile. -------------------------
from hexfinity.manifold_check import assert_two_manifold
verts = [tuple(v.co) for v in tile.data.vertices]
faces = [tuple(p.vertices) for p in tile.data.polygons]
assert_two_manifold(verts, faces)
print("padded tile is a valid manifold")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS FLORA PAD CHECK PASSED")
