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
from mathutils import Vector

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


def new_state(hover_tile, prev_open_world=None, run_id="run1"):
    """No `selected_tiles` parameter any more -- `_clip_to_hexes` now scans
    every generated tile in the map itself (`_generated_tiles`), so a run
    is no longer restricted to a pre-selected set."""
    state = types.SimpleNamespace()
    state._hover_tile = hover_tile
    state._prev_open_world = prev_open_world
    state._run_id = run_id
    state.type_name = "Bridge"
    state.report = lambda level, msg: print("report:", level, msg)
    cls = segment_path.HEXFINITY_OT_start_segments_path_draw
    # staticmethods take no implicit self -- bind those as plain functions,
    # everything else as a bound method on `state`.
    static_names = ("_world_bbox", "_shared_edge_idx", "_surface_z_at",
                    "_closest_point_on_segment", "_point_in_tile_local",
                    "_generated_tiles")
    for name in static_names:
        setattr(state, name, getattr(cls, name))
    for name in ("_clip_to_hexes", "_edge_endpoints_world", "_commit_piece",
                "_start_view_pan", "_connects_to_prev"):
        setattr(state, name, types.MethodType(cls.__dict__[name], state))
    return state


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
state._commit_piece(bpy.context, seg_short, Vector((0.0, 0.0, surface_z)), 0.0)
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
state._commit_piece(bpy.context, seg_long, midpoint_world, 0.0)

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
