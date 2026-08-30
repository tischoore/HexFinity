"""Headless smoke test: register the extension, generate a sloped single tile,
plant a tree, and exercise the pin/notch interlock end to end — including the
"only exists right after finalize" contract, the pin being a CHILD of its
tree (not a sibling under the tile), the tree/pin NOT sinking into the socket
after finalizing (the bug this check exists to catch), and the separate STL
export path.
Run with:
    blender --background --python tests/_headless_flora_pin_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.
"""
import os
import shutil
import sys
import tempfile

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
mp.smoothness_passes = 3
mp.resample_density = 0
mp.man_height_mm = 10.0
mp.grid_x = 0
mp.grid_y = 0

res = bpy.ops.hexfinity.generate_map()
assert res == {'FINISHED'}, res
tile = next(o for o in mp.root_collection.objects if o.hexfinity_tile.is_generated)
tprops = tile.hexfinity_tile

# A real slope, so the pad (and therefore the notch cut into it) has
# something real to flatten against.
for n, level in zip(("p1", "p2", "p3", "p4", "p5", "p6"), (0, 1, 2, 3, 4, 5)):
    setattr(tprops, n, level)

# Plant a tree near the tile centre, with a non-1.0 scale so the test proves
# the pin stays a fixed real-world size regardless of the tree's own random
# per-placement scale (bypasses the modal picker, same state
# HEXFINITY_OT_flora_marker._place_tree writes).
placement = tprops.flora_placements.add()
placement.species_file = "LeafyTree_Small_1.stl"
placement.tree_type = 'LEAFY_TREE'
placement.local_x_mm = 5.0
placement.local_y_mm = -5.0
placement.rotation_rad = 0.3
placement.scale_factor = 1.4

from hexfinity import operators, flora
from hexfinity.mesh_builder import FLORA_PIN_RADIUS_MM, FLORA_PIN_LENGTH_MM
from hexfinity.manifold_check import assert_two_manifold


def _tree_and_pin(tile_obj):
    tree_obj = next((c for c in tile_obj.children if c.get(flora.FLORA_OF)), None)
    pin_obj = None
    if tree_obj is not None:
        pin_obj = next((c for c in tree_obj.children if c.get(flora.FLORA_PIN_OF)), None)
    return tree_obj, pin_obj


def _true_base_world_z(tree_obj, min_z):
    return (tree_obj.matrix_world @ Vector((0.0, 0.0, min_z))).z


_, min_z, _hx, _hy, _lcx, _lcy, _br = flora._get_or_import_mesh(
    'LEAFY_TREE', 'LeafyTree_Small_1.stl')

# ---- Plain rebuild (no finalize): pad exists, pin/notch do not. -----------
operators.rebuild_tile(tile)
bpy.context.view_layer.update()
plain_verts = len(tile.data.vertices)
tree_obj, pin_obj = _tree_and_pin(tile)
assert tree_obj is not None
assert pin_obj is None, "a plain rebuild must not create a pin"
# Ground truth for the seating-correctness check below: no notch exists yet,
# so this raycast-derived base height is trustworthy as-is.
plain_base_z = _true_base_world_z(tree_obj, min_z)
print("plain rebuild: no pin, verts =", plain_verts,
     " tree true-base world z = %.4f" % plain_base_z)

# ---- Finalize: cuts the socket + creates exactly one correctly-tagged pin, -
# parented to the tree (not the tile), counter-scaled so its real-world size
# stays fixed regardless of the tree's own random scale_factor. -------------
operators.rebuild_tile(tile, finalize_flora=True)
finalized_verts = len(tile.data.vertices)
assert finalized_verts > plain_verts, (
    "finalizing should cut a socket and add geometry", plain_verts, finalized_verts)
# Blender doesn't propagate matrix_world through a freshly-created two-level
# parent chain (tile -> tree -> pin) until the view layer updates — same
# quirk operators.py already works around elsewhere (see its comment near
# "Flush the location change into matrix_world before we read it below").
bpy.context.view_layer.update()

tree_obj, pin_obj = _tree_and_pin(tile)
assert pin_obj is not None, "finalize should attach a pin"
assert pin_obj.parent is tree_obj, "pin must be a CHILD of its tree, not a sibling under the tile"
assert pin_obj[flora.FLORA_PLACEMENT_INDEX] == 0
assert tree_obj[flora.FLORA_PLACEMENT_INDEX] == 0

total_scale = placement.scale_factor * (mp.man_height_mm / flora.TREE_ASSET_MAN_HEIGHT_MM)
expected_inv_scale = 1.0 / total_scale
for s in pin_obj.scale:
    assert abs(s - expected_inv_scale) < 1e-6, (
        "pin's own scale must counter the tree's scale", tuple(pin_obj.scale), expected_inv_scale)

# World-space measurement (via matrix_world, not the raw local mesh coords)
# proves the tree-scale x pin-counter-scale product actually cancels out to
# exactly the hardcoded constants, regardless of scale_factor=1.4 here. Radius
# is measured as true radial distance from the pin's own world-space centre
# axis, NOT an axis-aligned bounding-box half-width — the pin's 12-gon
# vertices sit exactly on that circle, but the tree's 0.3rad rotation carries
# through to the pin, so a bounding box would (correctly, but misleadingly)
# undershoot whenever no vertex happens to land exactly on the X/Y axis.
world_verts = [pin_obj.matrix_world @ v.co for v in pin_obj.data.vertices]
cx, cy, _cz = pin_obj.matrix_world.translation
zs = [v.z for v in world_verts]
measured_radius = max(((v.x - cx) ** 2 + (v.y - cy) ** 2) ** 0.5 for v in world_verts)
measured_length = max(zs) - min(zs)
assert abs(measured_radius - FLORA_PIN_RADIUS_MM) < 1e-6, (
    "pin world-space radius must match the hardcoded constant regardless of tree scale",
    measured_radius, FLORA_PIN_RADIUS_MM)
assert abs(measured_length - FLORA_PIN_LENGTH_MM) < 1e-6, (
    measured_length, FLORA_PIN_LENGTH_MM)
print("finalize: 1 pin, child of its tree, world radius=%.3f length=%.3f (fixed, scale_factor=%.1f ignored)"
     % (measured_radius, measured_length, placement.scale_factor))

# ---- THE core regression check: the tree must still sit on the surface, ---
# not sunk ~FLORA_NOTCH_DEPTH_MM into the socket it just cut for itself. ----
# Tolerance is 0.05mm, not micron-tight: `notch_heights` reads the pad height
# from a notch-boundary vertex rather than the exact pad centre, and the pad
# blend transition can legitimately differ by a hair there — the regression
# this guards against (a broken raycast hitting the socket floor) is off by
# ~FLORA_NOTCH_DEPTH_MM (~10mm), two orders of magnitude larger.
finalized_base_z = _true_base_world_z(tree_obj, min_z)
assert abs(finalized_base_z - plain_base_z) < 0.05, (
    "tree sank after finalizing — the seating raycast is hitting the "
    "socket floor instead of the surrounding pad surface",
    plain_base_z, finalized_base_z)
print("seating check OK: tree true-base world z unchanged by finalizing (%.4f)"
     % finalized_base_z)

# The pin's own top must also land exactly at that same surface height,
# regardless of `penetration_mm` (see flora.sync_flora's derivation).
pin_top_world_z = (pin_obj.matrix_world @ Vector((0.0, 0.0, 0.0))).z
penetration_mm = scene.hexfinity_flora.penetration_mm
assert abs(pin_top_world_z - (finalized_base_z + penetration_mm)) < 1e-4, (
    "pin top must sit exactly at the (un-penetrated) socket mouth height",
    pin_top_world_z, finalized_base_z + penetration_mm)
print("pin anchoring OK: pin top at socket mouth height, decoupled from penetration_mm")

verts = [tuple(v.co) for v in tile.data.vertices]
faces = [tuple(p.vertices) for p in tile.data.polygons]
assert_two_manifold(verts, faces)
print("finalized tile is a valid manifold")

# ---- An unrelated rebuild afterward strips the pin/notch again — proves ---
# the "skip everywhere except finalize" contract holds end to end. ----------
operators.rebuild_tile(tile)   # e.g. a corner-height edit, brush stroke, ...
after_verts = len(tile.data.vertices)
tree_obj, pin_obj = _tree_and_pin(tile)
assert pin_obj is None, "an unrelated rebuild must strip the pin again"
assert after_verts == plain_verts, (
    "an unrelated rebuild must strip the notch geometry back to the plain-padded count",
    after_verts, plain_verts)
print("unrelated rebuild: pin/notch gone again, verts back to", after_verts)

# Re-finalize for the export check below.
operators.rebuild_tile(tile, finalize_flora=True)
tree_obj, pin_obj = _tree_and_pin(tile)

# ---- Export: tile STL excludes tree/pin geometry, flora STL exists, and --
# both files are separately valid to reimport, with the tree+pin STL's true
# base sitting at z=0 (not at the tree's arbitrary local mesh origin). ------
out_dir = tempfile.mkdtemp(prefix="hf_flora_pin_export_")
try:
    res = bpy.ops.hexfinity.export_tiles(
        'EXEC_DEFAULT', directory=out_dir, subfolder="out")
    assert res == {'FINISHED'}, res
    export_dir = os.path.join(out_dir, "out")
    files = os.listdir(export_dir)
    tile_stls = [f for f in files if f.startswith("hex_") and f.endswith(".stl")]
    flora_stls = [f for f in files if f.startswith("flora_") and f.endswith(".stl")]
    assert len(tile_stls) == 1, files
    assert len(flora_stls) == 1, files
    assert "flora_manifest.csv" in files, files
    print("export: tile STL =", tile_stls[0], " flora STL =", flora_stls[0])

    def _import_mesh(path):
        before = set(scene.objects)
        bpy.ops.wm.stl_import(filepath=path)
        imported = [o for o in scene.objects if o not in before]
        assert len(imported) == 1, imported
        obj = imported[0]
        tri_count = len(obj.data.polygons)
        min_z_export = min(v.co.z for v in obj.data.vertices)
        bpy.data.objects.remove(obj, do_unlink=True)
        return tri_count, min_z_export

    tile_export_tris, _ = _import_mesh(os.path.join(export_dir, tile_stls[0]))
    flora_export_tris, flora_min_z = _import_mesh(os.path.join(export_dir, flora_stls[0]))

    # Baseline: export the tile object ALONE (no children at all) through
    # the same STL exporter/triangulation, independent of the real operator's
    # child-filtering logic — the real per-tile export must match this
    # exactly, proving no tree/pin geometry leaked into it.
    for o in scene.objects:
        o.select_set(False)
    tile.select_set(True)
    bpy.context.view_layer.objects.active = tile
    baseline_path = os.path.join(out_dir, "baseline_tile_only.stl")
    bpy.ops.wm.stl_export(filepath=baseline_path, export_selected_objects=True,
                          apply_modifiers=True)
    tile.select_set(False)
    baseline_tris, _ = _import_mesh(baseline_path)

    assert tile_export_tris == baseline_tris, (
        "exported tile STL must not include tree/pin geometry",
        tile_export_tris, baseline_tris)
    assert flora_export_tris > 0
    assert abs(flora_min_z - 0.0) < 1e-4, (
        "exported tree+pin STL's true base must sit at z=0 for printing, "
        "not the tree's arbitrary local mesh origin", flora_min_z)
    print("export triangle counts OK — tile:", tile_export_tris,
         " (tile-alone baseline:", baseline_tris, ") flora (tree+pin):", flora_export_tris,
         " flora min z: %.5f" % flora_min_z)
finally:
    shutil.rmtree(out_dir, ignore_errors=True)

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS FLORA PIN CHECK PASSED")
