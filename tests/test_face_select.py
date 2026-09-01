import math

import pytest

import face_select as fs
from mesh_builder import build_hex_tile, top_vertex_count
from manifold_check import assert_two_manifold


def _flat_tile(**kw):
    base = dict(
        diameter_mm=220.0, level_height_mm=10.0, base_thickness_mm=10.0,
        corner_levels=(0, 0, 0, 0, 0, 0), center_level=None,
        smoothness_passes=2, resample_density=0,
    )
    base.update(kw)
    return build_hex_tile(**base)


def _domed_tile(**kw):
    base = dict(
        diameter_mm=220.0, level_height_mm=10.0, base_thickness_mm=10.0,
        corner_levels=(0, 0, 0, 0, 0, 0), center_level=6,
        smoothness_passes=3, resample_density=0,
    )
    base.update(kw)
    return build_hex_tile(**base)


# ---------------------------------------------------------------------------
# face_normal / adjacency
# ---------------------------------------------------------------------------
def test_face_normal_points_up_for_flat_top():
    verts, faces = _flat_tile()
    n = fs.face_normal(verts, faces[0])
    assert n[2] == pytest.approx(1.0, abs=1e-6)


def test_face_normal_degenerate_is_zero_vector():
    verts = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
    assert fs.face_normal(verts, (0, 1, 2)) == (0.0, 0.0, 0.0)


def test_build_face_adjacency_shared_edge_has_two_faces():
    faces = [(0, 1, 2), (1, 3, 2)]
    adj = fs.build_face_adjacency(faces)
    assert adj[fs._edge_key(1, 2)] == [0, 1]
    assert adj[fs._edge_key(0, 1)] == [0]


# ---------------------------------------------------------------------------
# flood_fill_faces
# ---------------------------------------------------------------------------
def test_flood_fill_flat_tile_selects_every_top_face_at_zero_threshold():
    verts, faces = _flat_tile()
    selected = fs.flood_fill_faces(verts, faces, seed_index=0, angle_threshold_deg=0.0)
    # Every top face is exactly coplanar on a flat tile, so a zero-tolerance
    # fill should reach all of them (and never leak onto walls/bottom/tab
    # geometry). "Top face" here means every vertex is in the 0..num_top-1
    # prefix build_hex_tile registers top-surface verts into (per the
    # CLAUDE.md invariant) — NOT just "any upward-facing face anywhere in
    # the mesh", since flat tab-hole floors are also horizontal/upward but
    # are separate, disconnected geometry the flood fill can never reach.
    num_top = top_vertex_count(2, 0)
    true_top_faces = [i for i, f in enumerate(faces) if all(v < num_top for v in f)]
    assert len(selected) == len(true_top_faces)


def test_flood_fill_never_includes_non_top_faces():
    verts, faces = _flat_tile()
    selected = fs.flood_fill_faces(verts, faces, seed_index=0, angle_threshold_deg=89.0)
    for i in selected:
        assert fs.face_normal(verts, faces[i])[2] > 0.05


def test_flood_fill_grows_with_threshold_on_domed_tile():
    verts, faces = _domed_tile()
    # Seed near the rim, far from the raised centre.
    seed = 0
    small = fs.flood_fill_faces(verts, faces, seed, angle_threshold_deg=1.0)
    large = fs.flood_fill_faces(verts, faces, seed, angle_threshold_deg=30.0)
    assert 0 < len(small) <= len(large)


def test_flood_fill_out_of_range_seed_is_empty():
    verts, faces = _flat_tile()
    assert fs.flood_fill_faces(verts, faces, len(faces) + 10, 10.0) == set()


def test_flood_fill_selection_is_connected():
    verts, faces = _domed_tile()
    selected = fs.flood_fill_faces(verts, faces, seed_index=0, angle_threshold_deg=5.0)
    adj = fs.build_face_adjacency(faces)
    seen = {0}
    frontier = [0]
    while frontier:
        i = frontier.pop()
        face = faces[i]
        for k in range(len(face)):
            a, b = face[k], face[(k + 1) % len(face)]
            for j in adj.get(fs._edge_key(a, b), ()):
                if j in selected and j not in seen:
                    seen.add(j)
                    frontier.append(j)
    assert seen == selected


# ---------------------------------------------------------------------------
# boundary_loop
# ---------------------------------------------------------------------------
def test_boundary_loop_single_triangle():
    faces = [(0, 1, 2)]
    loop = fs.boundary_loop(faces, {0})
    assert loop is not None
    assert set(loop) == {0, 1, 2}


def test_boundary_loop_flat_tile_full_selection_matches_rim_vertex_count():
    verts, faces = _flat_tile()
    num_top = top_vertex_count(2, 0)
    top_faces = [i for i, f in enumerate(faces) if all(v < num_top for v in f)]
    loop = fs.boundary_loop(faces, set(top_faces))
    assert loop is not None
    # The outer boundary of "every top face" is the tile's own hex rim: every
    # boundary vertex sits between the apothem (edge midpoints) and the
    # circumradius (corners), diameter_mm=220 -> radius 110.
    apothem = 110.0 * math.cos(math.pi / 6.0)
    for i in loop:
        x, y, _ = verts[i]
        r = math.hypot(x, y)
        assert apothem - 1e-3 <= r <= 110.0 + 1e-3


def test_boundary_loop_two_disjoint_triangles_returns_none():
    faces = [(0, 1, 2), (3, 4, 5)]
    assert fs.boundary_loop(faces, {0, 1}) is None


def test_boundary_loop_pinch_point_returns_none():
    # Two triangles sharing only a single vertex (0) -> that vertex has
    # boundary-edge degree 4, not 2.
    faces = [(0, 1, 2), (0, 3, 4)]
    assert fs.boundary_loop(faces, {0, 1}) is None


# ---------------------------------------------------------------------------
# loop_to_xy / end-to-end round trip through the existing region pipeline
# ---------------------------------------------------------------------------
def test_loop_to_xy_reads_tile_local_xy():
    verts = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]
    assert fs.loop_to_xy(verts, [1, 0]) == [(4.0, 5.0), (1.0, 2.0)]


def test_flood_fill_polygon_round_trips_through_build_hex_tile():
    verts, faces = _flat_tile()
    selected = fs.flood_fill_faces(verts, faces, seed_index=0, angle_threshold_deg=45.0)
    loop = fs.boundary_loop(faces, selected)
    assert loop is not None
    polygon = fs.loop_to_xy(verts, loop)
    assert len(polygon) >= 3

    region = [{
        "surface_type": "COBBLESTONE", "feature_mm": 12.0, "depth_mm": 2.0,
        "regularity": 0.4, "direction_rad": 0.0,
        "polygon": polygon, "mask_falloff_mm": 5.0,
    }]
    out_verts, out_faces = _flat_tile(surface_regions=region)
    assert_two_manifold(out_verts, out_faces)
