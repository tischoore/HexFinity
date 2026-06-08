"""Headless smoke test: register the extension, generate a small map, attach a
loose terrain-style object to the map collection, then exercise the destructive
Clear operator and assert the whole map is torn down. Run with:
    blender --background --python tests/_headless_clear_check.py
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
mp.grid_y = 2

# ---- Generate a 2x2 map -------------------------------------------------
res = bpy.ops.hexfinity.generate_map()
assert res == {'FINISHED'}, res
assert mp.is_generated is True
coll = mp.root_collection
assert coll is not None, "root_collection should be set after generate"
tiles = [o for o in coll.objects if o.hexfinity_tile.is_generated]
assert len(tiles) == 4, ("expected 4 tiles", len(tiles))
print("generate OK; tiles =", len(tiles), "collection =", coll.name)

# A loose terrain-style object linked into the map collection (mirrors how the
# import operator / scatter sync attach extra objects) — proves Clear removes
# everything in the collection, not just the tiles.
extra_mesh = bpy.data.meshes.new("TerrainBlob")
extra = bpy.data.objects.new("TerrainBlob", extra_mesh)
coll.objects.link(extra)
extra.parent = tiles[0]
assert extra.name in bpy.data.objects
print("attached loose terrain object:", extra.name)

# ---- Clear (EXEC_DEFAULT skips the invoke_confirm dialog) ---------------
res = bpy.ops.hexfinity.clear_map('EXEC_DEFAULT')
assert res == {'FINISHED'}, res

assert mp.is_generated is False, "is_generated should be reset by Clear"
assert mp.root_collection is None, "root_collection should be cleared"
assert mp.show_globals is False, "show_globals should be collapsed by Clear"
assert "HexFinity Map" not in bpy.data.collections, "map collection should be gone"
leftover_tiles = [o for o in bpy.data.objects
                  if o.name.startswith("HexTile_")]
assert not leftover_tiles, ("tiles should be deleted", leftover_tiles)
assert "TerrainBlob" not in bpy.data.objects, "loose terrain object should be deleted"
print("clear OK; objects left:", [o.name for o in bpy.data.objects])

# Regenerate after Clear to confirm we returned to a clean editable state.
res = bpy.ops.hexfinity.generate_map()
assert res == {'FINISHED'}, res
assert mp.is_generated is True
assert len([o for o in mp.root_collection.objects
            if o.hexfinity_tile.is_generated]) == 4
print("re-generate after clear OK")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS CLEAR CHECK PASSED")
