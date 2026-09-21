"""Headless smoke test: register the extension and exercise the Draw
Segments Path placement tool's non-interactive pieces in real bpy. Run with:
    blender --background --python tests/_headless_segments_path_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.

The real modal (mouse-driven hover/snap/rotate) can't be driven headlessly
-- same caveat as tests/_headless_path_feature_check.py and
tests/_headless_segment_add_check.py. This script instead binds
HEXFINITY_OT_start_segments_path_draw's private methods onto a plain
types.SimpleNamespace "self" (mirroring _headless_path_feature_check.py's
own state = types.SimpleNamespace() approach for
_resolve_crossing_neighbour/_continue_onto) and drives _commit_piece /
_clip_to_hexes / the Remove operator directly, with
segment_path._get_or_import_segment_mesh monkeypatched to hand back a
synthetic box mesh instead of importing a real STL file (mirrors
_headless_segment_add_check.py's make_square_segment() standing in for a
real import).
"""
import os
import sys
import types

import bpy
from mathutils import Vector, Matrix

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import hexfinity
from hexfinity import segment_path, operators
from hexfinity.map import NE, EDGE_DIRECTIONS, neighbour_coord, tile_world_xy

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

coll = bpy.data.collections.new("HexFinity Map")
scene.collection.children.link(coll)
mp.root_collection = coll


def make_tile(name, q, r):
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    coll.objects.link(obj)
    tile = obj.hexfinity_tile
    tile.coord_q, tile.coord_r = q, r
    for n in ("p1", "p2", "p3", "p4", "p5", "p6"):
        setattr(tile, n, 2)
    tile.is_generated = True
    obj.location.x, obj.location.y = tile_world_xy(q, r, mp.diameter_mm)
    bpy.context.view_layer.update()
    operators.rebuild_tile(obj)
    return obj


tile0 = make_tile("HexTile_seg_0", 0, 0)
edge_idx = EDGE_DIRECTIONS.index(NE)
nq, nr = neighbour_coord(0, 0, NE)
tile1 = make_tile("HexTile_seg_1", nq, nr)
print("two adjacent tiles built OK")

surface_z = mp.base_thickness_mm + 2 * mp.level_height_mm


def make_box_mesh(name, length_mm, width_mm=20.0, height_mm=6.0):
    """A flat box centred on the origin, its long axis along local X --
    stands in for an imported segment STL, mirroring
    _headless_segment_add_check.py's make_square_segment()."""
    hl, hw, hh = length_mm / 2.0, width_mm / 2.0, height_mm / 2.0
    verts = [
        (-hl, -hw, 0.0), (hl, -hw, 0.0), (hl, hw, 0.0), (-hl, hw, 0.0),
        (-hl, -hw, height_mm), (hl, -hw, height_mm),
        (hl, hw, height_mm), (-hl, hw, height_mm),
    ]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
            (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)
    return mesh


_fake_meshes = {}


def fake_get_or_import_segment_mesh(filepath):
    return _fake_meshes.get(filepath)


segment_path._get_or_import_segment_mesh = fake_get_or_import_segment_mesh


def new_state(hover_tile, open_connectors=None, snap_meta=None, run_id="run1"):
    """No `selected_tiles` parameter any more -- `_clip_to_hexes` now scans
    every generated tile in the map itself (`_generated_tiles`), so a run
    is no longer restricted to a pre-selected set.

    `open_connectors`/`snap_meta` stand in for what a real hover/snap
    (_update_hover) would have left on `self` -- `_commit_piece` reads
    `self._snap_meta` to know which specific open connector this
    placement consumed, and `_connects_to_prev` checks it against
    `self._open_connectors`."""
    state = types.SimpleNamespace()
    state._hover_tile = hover_tile
    state._open_connectors = open_connectors if open_connectors is not None else []
    state._snap_meta = snap_meta
    state._run_id = run_id
    state.type_name = "Bridge"
    state.report = lambda level, msg: print("report:", level, msg)
    state._edge_snap_cache_tile = None
    state._edge_snap_cache_targets = []
    state._ghost_tri_cache = {}
    cls = segment_path.HEXFINITY_OT_start_segments_path_draw
    # staticmethods take no implicit self -- bind those as plain functions,
    # everything else as a bound method on `state`.
    static_names = ("_world_bbox", "_shared_edge_idx", "_surface_z_at",
                    "_closest_point_on_segment", "_point_in_tile_local",
                    "_generated_tiles")
    for name in static_names:
        setattr(state, name, getattr(cls, name))
    for name in ("_clip_to_hexes", "_edge_endpoints_world", "_commit_piece",
                "_start_view_pan", "_connects_to_prev", "_fit_placement",
                "_tile_edge_snap_targets", "_local_triangles"):
        setattr(state, name, types.MethodType(cls.__dict__[name], state))
    return state


IDENTITY_ROTATION = Matrix.Rotation(0.0, 3, 'Z')


# ---------------------------------------------------------------------------
# Case A: a short segment placed entirely within one generated hex -- no
# boolean split, one piece, parented + listed on that tile alone. Neither
# tile0 nor tile1 is selected anywhere in this script until the Remove test
# at the very end, proving placement/clipping no longer depends on
# pre-selection.

_fake_meshes["short.stl"] = make_box_mesh("HF_Segment_short", 40.0)
seg_short = {
    "file": "short.stl",
    "waypoints": [
        {"x_mm": -20.0, "y_mm": 0.0, "edge_idx": -1},
        {"x_mm": 20.0, "y_mm": 0.0, "edge_idx": -1},
    ],
}
state = new_state(tile0)
before = len(tile0.hexfinity_tile.path_features)
state._commit_piece(bpy.context, seg_short, Vector((0.0, 0.0, surface_z)), IDENTITY_ROTATION)
tile0_features = tile0.hexfinity_tile.path_features
assert len(tile0_features) == before + 1, len(tile0_features)
feat = tile0_features[before]
assert feat.feature_type == 'SEGMENT', feat.feature_type
assert feat.segment_piece is not None
piece = feat.segment_piece
assert piece.parent is tile0, piece.parent
assert piece.get(segment_path.SEGMENT_PIECE_TAG) is True
assert piece.get(segment_path.SEGMENT_TYPE_NAME) == "Bridge"
print("single-hex placement: parented + listed OK ->", feat.name)

assert operators._is_terrain_object(piece) is False
assert piece not in operators._terrain_objects(tile0)
print("segment piece excluded from _terrain_objects/_is_terrain_object OK")

# ---------------------------------------------------------------------------
# Case B: a long segment whose bounding box spans both generated hexes ->
# boolean-INTERSECT split into two non-empty, correctly parented pieces,
# with tile1 auto-continued onto despite never being selected.

span_length = mp.diameter_mm * 1.4
_fake_meshes["long.stl"] = make_box_mesh("HF_Segment_long", span_length)
seg_long = {
    "file": "long.stl",
    "waypoints": [
        {"x_mm": -span_length / 2.0, "y_mm": 0.0, "edge_idx": -1},
        {"x_mm": span_length / 2.0, "y_mm": 0.0, "edge_idx": -1},
    ],
}
midpoint_world = (Vector(tile0.location[:]) + Vector(tile1.location[:])) / 2.0
midpoint_world.z = surface_z

state = new_state(tile0)
before0 = len(tile0.hexfinity_tile.path_features)
before1 = len(tile1.hexfinity_tile.path_features)
state._commit_piece(bpy.context, seg_long, midpoint_world, IDENTITY_ROTATION)

f0 = tile0.hexfinity_tile.path_features
f1 = tile1.hexfinity_tile.path_features
assert len(f0) == before0 + 1, len(f0)
assert len(f1) == before1 + 1, len(f1)
p0 = f0[before0].segment_piece
p1 = f1[before1].segment_piece
assert p0 is not None and p1 is not None
assert p0.parent is tile0 and p1.parent is tile1
assert len(p0.data.polygons) > 0 and len(p1.data.polygons) > 0
assert tile1.select_get(), "crossing neighbour should be auto-selected"
print("crossing neighbour auto-selected despite no pre-selection OK")
print("cross-hex placement: boolean-split into two parented pieces OK ->",
      f0[before0].name, "/", f1[before1].name)

# ---------------------------------------------------------------------------
# Case C: _fit_placement tilts a piece so its authored Corners land close
# to a sloped tile's real surface -- and closer than a flat (untilted)
# placement at the same XY would.

tile2 = make_tile("HexTile_seg_2", 5, 5)
t2 = tile2.hexfinity_tile
t2.p1, t2.p2, t2.p3, t2.p4, t2.p5, t2.p6 = 0, 2, 4, 1, 3, 5
bpy.context.view_layer.update()
operators.rebuild_tile(tile2)

seg_tilt = {
    "file": "short.stl",
    "waypoints": [
        {"x_mm": -20.0, "y_mm": 0.0, "edge_idx": -1},
        {"x_mm": 20.0, "y_mm": 0.0, "edge_idx": -1},
    ],
    "corners_local_mm": [[-15.0, -15.0], [15.0, -15.0], [0.0, 15.0]],
}
state = new_state(tile2)
flat_translation = Vector((tile2.location.x, tile2.location.y, surface_z))
translation, rotation = state._fit_placement(
    bpy.context, tile2, seg_tilt, flat_translation, 0.0, None, None)

assert rotation != IDENTITY_ROTATION, "expected a non-flat (tilted) rotation"

flat_errors, tilt_errors, signed_tilt_errors = [], [], []
for (x, y) in seg_tilt["corners_local_mm"]:
    world = translation + rotation @ Vector((x, y, 0.0))
    real_z = state._surface_z_at(bpy.context, tile2, world.x, world.y)
    signed_tilt_errors.append(world.z - real_z)
    tilt_errors.append(abs(world.z - real_z))
    flat_errors.append(abs(flat_translation.z - real_z))

print("Case C corner errors -- flat:", flat_errors, "tilted:", tilt_errors)
assert sum(tilt_errors) < sum(flat_errors), (flat_errors, tilt_errors)
assert max(tilt_errors) < 3.0, tilt_errors
assert all(e >= -1e-3 for e in signed_tilt_errors), (
    "expected no corner to sink below the surface", signed_tilt_errors)
print("corner-fit tilt places corners closer to the sloped surface, "
      "never below it, OK")

# A segment authored before Corners existed (no corners_local_mm key) must
# fall back to the old flat/yaw-only placement rather than raising.
seg_no_corners = dict(seg_tilt)
del seg_no_corners["corners_local_mm"]
fallback_translation, fallback_rotation = state._fit_placement(
    bpy.context, tile2, seg_no_corners, flat_translation, 0.0, None, None)
assert fallback_translation == flat_translation
assert fallback_rotation == IDENTITY_ROTATION
print("segment with no authored corners falls back to a flat placement OK")

# ---------------------------------------------------------------------------
# Case D: a 4-corner footprint on genuinely hilly (non-planar) terrain. A
# rigid tilt alone can touch at most 3 corners exactly (fit_resting_plane),
# so it generically leaves one corner floating a small gap above the
# surface -- an accepted trade-off, not something this module corrects
# with a mesh deformation (see the module docstring): the segment's own
# mesh must stay exactly the rigid prefab it was authored as. What must
# never happen is a corner sinking *below* the surface (visibly buried
# into the terrain) -- unlike a least-squares fit, which would split the
# error both ways.

tile3 = make_tile("HexTile_seg_3", 7, 7)
t3 = tile3.hexfinity_tile
t3.p1, t3.p2, t3.p3, t3.p4, t3.p5, t3.p6 = 0, 6, 0, 6, 0, 6
bpy.context.view_layer.update()
operators.rebuild_tile(tile3)

seg_saddle = {
    "file": "short.stl",
    "waypoints": [
        {"x_mm": -20.0, "y_mm": 0.0, "edge_idx": -1},
        {"x_mm": 20.0, "y_mm": 0.0, "edge_idx": -1},
    ],
    "corners_local_mm": [[-40.0, -40.0], [40.0, -40.0], [40.0, 40.0], [-40.0, 40.0]],
}
state = new_state(tile3)
flat_translation3 = Vector((tile3.location.x, tile3.location.y, surface_z))
translation3, rotation3 = state._fit_placement(
    bpy.context, tile3, seg_saddle, flat_translation3, 0.0, None, None)

flat_errors3, tilt_errors3 = [], []
for (x, y) in seg_saddle["corners_local_mm"]:
    world = translation3 + rotation3 @ Vector((x, y, 0.0))
    real_z = state._surface_z_at(bpy.context, tile3, world.x, world.y)
    tilt_errors3.append(world.z - real_z)
    flat_errors3.append(flat_translation3.z - real_z)

print("Case D corner errors (mm) -- flat:", flat_errors3, "tilted:", tilt_errors3)
# Tight tolerance: this checks the corners' *true* final world position
# (including the tilt's own second-order effect on X/Y, not just Z) --
# exactly what _fit_placement's own verification-and-lift pass guards, so
# it should never go negative beyond floating-point noise.
assert all(e >= -1e-3 for e in tilt_errors3), (
    "expected the resting-plane tilt to never sink a corner below the "
    "surface at its own true final position", tilt_errors3)
assert any(e > 0.05 for e in tilt_errors3), (
    "expected at least one corner to still float above the surface on a "
    "4-corner saddle footprint -- raise the p1..p6 level spread in this "
    "test if it doesn't", tilt_errors3)
assert sum(abs(e) for e in tilt_errors3) < sum(abs(e) for e in flat_errors3)
print("4-corner rigid tilt on hilly terrain never sinks a corner below the "
      "surface, only floats one above, and improves on flat OK")

# ---------------------------------------------------------------------------
# Case E: a piece can leave *more than one* end open at once -- the very
# first piece of a run, before either end has been consumed by a join. Every
# one of them must be a valid target for continuing the chain, not just
# whichever one happened to be tracked -- regression test for a bug where
# only a single arbitrarily-chosen open connector was ever honoured, so
# snapping onto the *other* legitimate open end looked fine (the ghost
# turned green) but was silently rejected at placement time.

tile4 = make_tile("HexTile_seg_4", 9, 9)
bpy.context.view_layer.update()
operators.rebuild_tile(tile4)

_fake_meshes["chain.stl"] = make_box_mesh("HF_Segment_chain", 40.0)
seg_chain = {
    "file": "chain.stl",
    "waypoints": [
        {"x_mm": -20.0, "y_mm": 0.0, "edge_idx": 0},
        {"x_mm": 20.0, "y_mm": 0.0, "edge_idx": 0},
    ],
}

state = new_state(tile4)
flat_translation4 = Vector((tile4.location.x, tile4.location.y, surface_z))
state._commit_piece(bpy.context, seg_chain, flat_translation4, IDENTITY_ROTATION)

assert len(state._open_connectors) == 2, (
    "expected both ends of the first (as yet unconsumed) piece to stay open",
    len(state._open_connectors))
first_open, second_open = state._open_connectors[0], state._open_connectors[1]

state._snap_meta = ('open', first_open)
assert state._connects_to_prev() is True, (
    "expected the first open connector to be a valid continuation target")
state._snap_meta = ('open', second_open)
assert state._connects_to_prev() is True, (
    "expected the second open connector to be a valid continuation target too")

# Continue the chain by snapping onto the *first* open connector
# specifically -- the one an implementation that only ever tracks "the
# last" open connector would wrongly reject.
state._snap_meta = ('open', first_open)
next_translation = first_open["world"] + Vector((20.0, 0.0, 0.0))
state._commit_piece(bpy.context, seg_chain, next_translation, IDENTITY_ROTATION)

remaining_worlds = [oc["world"] for oc in state._open_connectors]
assert all((w - first_open["world"]).length > 1e-3 for w in remaining_worlds), (
    "expected the connector actually snapped onto (first_open) to be "
    "consumed, not left dangling", remaining_worlds)
print("continuing a chain from any open connector of the previous piece "
      "(not just a single tracked one) OK")

# ---------------------------------------------------------------------------
# Case F: _tile_edge_snap_targets caches per hovered tile -- a repeat call
# for the same tile must add zero further _surface_z_at raycasts, and a
# hovered-tile change must invalidate the cache (mirrors regions.py's own
# "only re-extract when the hovered tile changes" idiom).

state = new_state(tile0)
call_count = {"n": 0}
real_surface_z_at = state._surface_z_at


def counting_surface_z_at(context, tile, x, y, depsgraph=None):
    call_count["n"] += 1
    return real_surface_z_at(context, tile, x, y, depsgraph)


state._surface_z_at = counting_surface_z_at

first = state._tile_edge_snap_targets(bpy.context, tile0)
first_count = call_count["n"]
assert first_count == 12, first_count  # edge_snap=3 -> 6*(3-1)=12 points
second = state._tile_edge_snap_targets(bpy.context, tile0)
assert call_count["n"] == first_count, (
    "expected a repeat call for the same tile to add zero raycasts", call_count["n"])
assert second is first, "expected the exact cached list object back, not a recompute"
print(f"_tile_edge_snap_targets caches per hovered tile OK -> "
      f"{first_count} raycasts once, 0 more on repeat")

state._tile_edge_snap_targets(bpy.context, tile1)
assert call_count["n"] == first_count + 12, (
    "expected a hovered-tile change to recompute", call_count["n"])
print("cache invalidates when the hovered tile changes OK")

# ---------------------------------------------------------------------------
# Case G: the ghost preview's triangle count is driven by the segment's
# authored hull, not by how many triangles the real imported STL has.

def make_big_grid_mesh(name, size_mm=40.0, subdivisions=30):
    """A flat, densely subdivided grid -- stands in for a genuinely
    high-poly authored STL (subdivisions**2 * 2 triangles)."""
    n = subdivisions
    step = size_mm / n
    verts = [
        (i * step - size_mm / 2.0, j * step - size_mm / 2.0, 0.0)
        for j in range(n + 1) for i in range(n + 1)
    ]
    faces = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            b, c, d = a + 1, a + (n + 1), a + (n + 1) + 1
            faces.append((a, b, d, c))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)
    return mesh


_fake_meshes["big.stl"] = make_big_grid_mesh("HF_Segment_big")
seg_big = {
    "file": "big.stl",
    "waypoints": [
        {"x_mm": -20.0, "y_mm": 0.0, "edge_idx": -1},
        {"x_mm": 20.0, "y_mm": 0.0, "edge_idx": -1},
    ],
    "hull_local_mm": [[-20.0, -20.0], [20.0, -20.0], [20.0, 20.0], [-20.0, 20.0]],
}
real_triangle_count = 2 * 30 * 30
state = new_state(tile0)
ghost_tris = state._local_triangles(seg_big)
print("Case G ghost triangle count:", len(ghost_tris),
      "vs real mesh triangles:", real_triangle_count)
assert len(ghost_tris) < 50, (
    "expected a small hull-prism ghost regardless of the real mesh's size",
    len(ghost_tris))
assert len(ghost_tris) < real_triangle_count
print("ghost preview triangle count is driven by the authored hull, not mesh size OK")

# A segment with no authored hull must still fall back to the real mesh's
# own triangles (backward compatible with a pre-hull settings.json entry).
state2 = new_state(tile0)
fallback_tris = state2._local_triangles(seg_short)
assert len(fallback_tris) == 12, len(fallback_tris)  # make_box_mesh's 6 quads -> 12 tris
print("segment with no authored hull falls back to the real mesh's triangles OK")

# ---------------------------------------------------------------------------
# Case H: _snap_effective_dist -- the pixel-radius/world-floor snap
# qualification rule _update_hover relies on. Plain floats in, no bpy
# dependency, so this is checked directly without any viewport machinery.

RADIUS_PX = 26.0
FLOOR_MM = 2.0

# Comfortably within the pixel radius -> qualifies at its own pixel distance.
assert segment_path._snap_effective_dist(10.0, 50.0, RADIUS_PX, FLOOR_MM) == 10.0

# Beyond the pixel radius and beyond the world floor -> disqualified.
assert segment_path._snap_effective_dist(100.0, 50.0, RADIUS_PX, FLOOR_MM) is None

# Beyond the pixel radius (e.g. zoomed in close, so a small world distance
# projects to a large pixel distance) but within the world floor -> still
# qualifies, clamped to radius_px so it can't out-rank a genuinely
# pixel-closer candidate.
assert segment_path._snap_effective_dist(999.0, 1.0, RADIUS_PX, FLOOR_MM) == RADIUS_PX

# Exactly at the world floor boundary -> qualifies (inclusive).
assert segment_path._snap_effective_dist(999.0, FLOOR_MM, RADIUS_PX, FLOOR_MM) == RADIUS_PX

# A candidate already within the pixel radius (and also within the world
# floor) keeps its own true (smaller) pixel distance -- the clamp only ever
# lowers an out-of-pixel-radius distance down to radius_px, never raises an
# already-close one up to it.
assert segment_path._snap_effective_dist(5.0, 1.0, RADIUS_PX, FLOOR_MM) == 5.0
print("_snap_effective_dist qualification rule OK")

# ---------------------------------------------------------------------------
# Remove: tearing down a SEGMENT feature must also delete its piece object.

idx = before0
tile0.hexfinity_tile.active_path_feature_index = idx
piece_name = p0.name
bpy.context.view_layer.objects.active = tile0
tile0.select_set(True)
bpy.ops.hexfinity.remove_path_feature()
assert len(tile0.hexfinity_tile.path_features) == before0, \
    len(tile0.hexfinity_tile.path_features)
assert piece_name not in bpy.data.objects, piece_name
print("remove_path_feature tears down the piece object OK")

hexfinity.unregister()
print("unregister() OK")
print("HEADLESS SEGMENTS PATH CHECK PASSED")
