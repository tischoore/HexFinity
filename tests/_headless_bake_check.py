"""Headless smoke test: register the extension and exercise
hexfinity.bake_tile / hexfinity.unbake_tile end to end — brush strokes,
a planted tree (pad + pin/notch), and a path feature all frozen together,
an unrelated live edit (Surface Texture) leaving the freeze untouched, a
corner-height edit invalidating just the pad/terrain/notch/path portion
(not the frozen brush), and un-baking restoring live recompute. Run with:
    blender --background --python tests/_headless_bake_check.py
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

from hexfinity import operators
from hexfinity.mesh_builder import top_vertex_count, effective_resample

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
bpy.context.view_layer.objects.active = tile
tile.select_set(True)

# A real slope, so pad flattening / brush painting both have something to do.
for n, level in zip(("p1", "p2", "p3", "p4", "p5", "p6"), (0, 1, 2, 3, 4, 5)):
    setattr(tprops, n, level)

# ---- Terrain brush: write the persisted per-top-vertex layer directly
# (bypassing the modal paint operator) and rebuild to apply it. ------------
resample = effective_resample(mp.resample_density, tprops.local_subdiv)
num_top = top_vertex_count(mp.smoothness_passes, resample)
tile["hf_brush_disp"] = [1.5] * num_top
operators.rebuild_tile(tile)
assert list(tile.get("hf_brush_disp")) == [1.5] * num_top
print("brush stroke applied OK")

# ---- Plant a tree (bypasses the modal picker, mirrors _headless_flora_pad_check.py) --
placement = tprops.flora_placements.add()
placement.species_file = "LeafyTree_Small_1.stl"
placement.tree_type = 'LEAFY_TREE'
placement.local_x_mm = 5.0
placement.local_y_mm = -5.0
placement.rotation_rad = 0.3
placement.scale_factor = 1.0
operators.rebuild_tile(tile)
print("tree planted OK")

# ---- A path feature line (points added directly, type set last so its
# autofill/name/rebuild callback fires exactly once, per path_features.py's
# documented "set the type last" contract). --------------------------------
feature = tprops.path_features.add()
p1 = feature.points.add(); p1.x, p1.y = -30.0, 0.0
p2 = feature.points.add(); p2.x, p2.y = 30.0, 0.0
feature.feature_type = 'FOOTPATH'
assert len(feature.points) == 2
print("path feature drawn OK")

verts_before_bake = len(tile.data.vertices)
print("verts before bake:", verts_before_bake)

# Planting a tree imports its species STL via bpy.ops.wm.stl_import under the
# hood (flora._get_or_import_mesh), which — as an unrelated side effect of
# that *operator* — reassigns the active object to the (immediately-discarded)
# import target, leaving view_layer.objects.active dangling/None. Restore the
# selection before driving any more hexfinity.* operators, same as a real user
# re-clicking the tile in the Outliner after planting.
bpy.context.view_layer.objects.active = tile
tile.select_set(True)

# ---- Bake ------------------------------------------------------------------
res = bpy.ops.hexfinity.bake_tile()
assert res == {'FINISHED'}, res
assert tprops.is_baked
assert tile.get("hf_bake_sig") is not None
assert tile.get("hf_baked_extra_verts") is not None
assert tile.get("hf_baked_top_offset") is not None
assert tile.get("hf_brush_disp") is None, "brush layer should be folded+cleared on bake"
pin = next((c for c in tile.children_recursive if c.name.startswith("FloraPin_")), None)
assert pin is not None, "baking should finalize flora (cut the pin/notch)"
verts_after_bake = len(tile.data.vertices)
print("baked OK — verts after bake:", verts_after_bake, "(pin:", pin.name, ")")

# ---- An unrelated LIVE edit (Surface Texture / displacement) must NOT
# disturb the frozen pad/terrain/notch/path layer. --------------------------
tprops.surface_texture.surface_type = 'PLAINS'
assert tile.get("hf_bake_sig") is not None, \
    "a Surface Texture edit must not invalidate the bake"
assert len(tile.data.vertices) == verts_after_bake, \
    "surface texture is a top-vertex-only live layer — vertex count must be unchanged"
print("unrelated Surface Texture edit left the bake intact OK")

# ---- A corner-height edit invalidates just the pad/terrain/notch/path
# portion; the frozen brush offset must survive. -----------------------------
tprops.p1 = 2
assert tile.get("hf_bake_sig") is None, \
    "a corner edit must invalidate the frozen pad/terrain/notch/path layer"
assert tile.get("hf_baked_extra_verts") is None
assert tile.get("hf_baked_top_offset") is not None, \
    "the frozen brush offset must survive a corner-height edit"
assert tprops.is_baked, "is_baked stays True — only the pad/terrain/notch/path portion reverted"
print("corner edit correctly invalidated the pad/terrain/notch/path portion only OK")

# ---- Un-bake restores live recompute and is fully reversible. -------------
bpy.context.view_layer.objects.active = tile
tile.select_set(True)
res = bpy.ops.hexfinity.unbake_tile()
assert res == {'FINISHED'}, res
assert not tprops.is_baked
assert tile.get("hf_baked_top_offset") is None
assert tile.get("hf_brush_disp") is not None, "un-baking restores the brush offset to a live layer"
print("un-bake OK")

hexfinity.unregister()
print("unregister() OK")
print("ALL BAKE CHECKS PASSED")
