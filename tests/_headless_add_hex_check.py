"""Headless smoke test: register the extension, generate a small map, then
exercise hexfinity.add_adjacent_hex (the operator the add-hex gizmos invoke)
directly via bpy.ops — proving the new tile's corners weld onto whichever
existing neighbours border it and fall back to base_level elsewhere, and
that re-adding at an already-occupied coordinate is rejected. Run with:
    blender --background --python tests/_headless_add_hex_check.py
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
mp.base_level = 3
mp.grid_x = 2
mp.grid_y = 2

# ---- Generate a 2x2 map -------------------------------------------------
res = bpy.ops.hexfinity.generate_map()
assert res == {'FINISHED'}, res
coll = mp.root_collection


def _tile_at(q, r):
    for o in coll.objects:
        tp = o.hexfinity_tile
        if tp.is_generated and tp.coord_q == q and tp.coord_r == r:
            return o
    return None


tile00 = _tile_at(0, 0)
tile10 = _tile_at(1, 0)
assert tile00 is not None and tile10 is not None
print("generate OK; tile00 =", tile00.name, "tile10 =", tile10.name)

# Give a few corners distinctive, non-base_level values. P3 on both (0,0) and
# (1,0) has no existing neighbour in this 2x2 map (safe to set independently);
# P2 on (0,0) is shared with (1,0).P4 via NE and will cascade there too, which
# is fine and not asserted on.
tile00.hexfinity_tile.p2 = 9
tile00.hexfinity_tile.p3 = 11
tile10.hexfinity_tile.p3 = 7

# (1, -1) is an open slot bordering both (0,0) (via NW) and (1,0) (via N) —
# see map.SHARED_CORNERS / tests/test_map.py's
# test_resolve_new_tile_corners_pulls_from_two_neighbours_at_once for the
# hand-derived per-corner mapping this mirrors.
res = bpy.ops.hexfinity.add_adjacent_hex(coord_q=1, coord_r=-1)
assert res == {'FINISHED'}, res

new_tile = _tile_at(1, -1)
assert new_tile is not None, "new tile should be findable by its coord"
tp = new_tile.hexfinity_tile
assert tp.p1 == 7, ("P1 should pull from (1,0).P3", tp.p1)
assert tp.p2 == mp.base_level, ("P2 has no existing neighbour", tp.p2)
assert tp.p3 == mp.base_level, ("P3 has no existing neighbour", tp.p3)
assert tp.p4 == mp.base_level, ("P4 has no existing neighbour", tp.p4)
assert tp.p5 == 11, ("P5 should pull from (0,0).P3", tp.p5)
assert tp.p6 == 9, ("P6 should pull from (0,0).P2", tp.p6)
print("add_adjacent_hex OK; new tile corners =",
      (tp.p1, tp.p2, tp.p3, tp.p4, tp.p5, tp.p6))

# ---- Re-adding at the same coordinate must be rejected, not duplicated --
before = len([o for o in coll.objects if o.hexfinity_tile.is_generated])
res = bpy.ops.hexfinity.add_adjacent_hex(coord_q=1, coord_r=-1)
assert res == {'CANCELLED'}, res
after = len([o for o in coll.objects if o.hexfinity_tile.is_generated])
assert after == before, ("tile count should be unchanged", before, after)
print("duplicate add_adjacent_hex correctly rejected; tile count =", after)

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS ADD-HEX CHECK PASSED")
