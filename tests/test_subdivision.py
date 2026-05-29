import math

import pytest

from subdivision import (
    subdivide_loop,
    linear_midpoint_subdivide,
    _beta,
)
from mesh_builder import build_hex_tile, INNER_RING_FACTOR


# ---------------------------------------------------------------------------
# Control-mesh fixture matching what `build_hex_tile` constructs internally.

def _hex_control_mesh(
    corner_levels=(0, 0, 0, 0, 0, 0),
    center_level=None,
    diameter_mm=100.0,
    level_height_mm=5.0,
    base_thickness_mm=10.0,
    center_xy=(0.0, 0.0),
):
    """Build the 13-vert / 18-face control mesh + the six sharp rim edges."""
    R = diameter_mm / 2.0
    levels = tuple(max(0, int(L)) for L in corner_levels)
    corners_xy = []
    for i in range(6):
        a = math.pi / 3.0 - i * (math.pi / 3.0)
        corners_xy.append((R * math.cos(a), R * math.sin(a)))
    corner_z = [base_thickness_mm + levels[i] * level_height_mm for i in range(6)]
    if center_level is not None:
        center_z = base_thickness_mm + max(0, int(center_level)) * level_height_mm
    else:
        center_z = sum(corner_z) / 6.0
    C = (float(center_xy[0]), float(center_xy[1]), center_z)
    P = [(corners_xy[i][0], corners_xy[i][1], corner_z[i]) for i in range(6)]
    Q = []
    for i in range(6):
        ip1 = (i + 1) % 6
        mx = 0.5 * (P[i][0] + P[ip1][0])
        my = 0.5 * (P[i][1] + P[ip1][1])
        qx = C[0] + INNER_RING_FACTOR * (mx - C[0])
        qy = C[1] + INNER_RING_FACTOR * (my - C[1])
        qz = (P[i][2] + P[ip1][2] + C[2]) / 3.0
        Q.append((qx, qy, qz))
    verts = [C] + P + Q
    # C=0, P_i=1+i, Q_i=7+i.
    faces = []
    for i in range(6):
        ip1 = (i + 1) % 6
        faces.append((1 + i, 7 + i, 1 + ip1))
        faces.append((1 + i, 0,     7 + i))
        faces.append((7 + i, 0,     1 + ip1))
    sharp = [(1 + i, 1 + ((i + 1) % 6)) for i in range(6)]
    return verts, faces, sharp


# ---------------------------------------------------------------------------
# Loop-subdivision counts derived from the recurrence:
#   V_new = V_old + E_old
#   F_new = 4 · F_old
#   E_new = 2 · E_old + 3 · F_old
# Base disk: V=13, F=18, E=30.

@pytest.mark.parametrize(
    "levels,expected_v,expected_f",
    [
        (0,   13,   18),
        (1,   43,   72),
        (2,  157,  288),
        (3,  601, 1152),
    ],
)
def test_loop_subdivision_vertex_count_grows_correctly(
    levels, expected_v, expected_f
):
    verts, faces, sharp = _hex_control_mesh()
    out_verts, out_faces, _ = subdivide_loop(verts, faces, sharp, levels)
    assert len(out_verts) == expected_v
    assert len(out_faces) == expected_f


# ---------------------------------------------------------------------------
# Disk-manifold check: interior edges have 2 incident faces, boundary edges
# (the rim) have 1. This lets us verify the subdivided top surface on its
# own — it's an open disk, not the closed tile of `assert_two_manifold`.

def _assert_disk_manifold(verts, faces):
    edge_count = {}
    referenced = set()
    for face in faces:
        assert len(face) == 3, f"non-triangle face {face}"
        a, b, c = face
        assert a != b and b != c and a != c, f"degenerate triangle {face}"
        for x, y in ((a, b), (b, c), (c, a)):
            key = (x, y) if x < y else (y, x)
            edge_count[key] = edge_count.get(key, 0) + 1
            referenced.add(x)
            referenced.add(y)
    bad = [(e, c) for e, c in edge_count.items() if c not in (1, 2)]
    assert not bad, f"edges with bad face counts: {bad[:5]}"
    orphans = sorted(set(range(len(verts))) - referenced)
    assert not orphans, f"orphan verts: {orphans[:10]}"


@pytest.mark.parametrize("levels", [0, 1, 2, 3])
def test_subdivided_mesh_is_manifold(levels):
    verts, faces, sharp = _hex_control_mesh(corner_levels=(0, 1, 2, 1, 0, 0))
    out_verts, out_faces, _ = subdivide_loop(verts, faces, sharp, levels)
    _assert_disk_manifold(out_verts, out_faces)


def test_sharp_edges_stay_straight():
    # Every vertex generated along an originally-sharp edge must be colinear
    # with that edge's two endpoints — the side wall and the inter-tile rim
    # seam both rely on this.
    verts, faces, sharp = _hex_control_mesh(corner_levels=(0, 3, 0, 5, 1, 2))
    out_verts, _, rim_chains = subdivide_loop(verts, faces, sharp, 3)
    for chain in rim_chains:
        a = out_verts[chain[0]]
        b = out_verts[chain[-1]]
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        ab_len = math.sqrt(sum(c * c for c in ab))
        for mid_idx in chain[1:-1]:
            m = out_verts[mid_idx]
            am = (m[0] - a[0], m[1] - a[1], m[2] - a[2])
            # m is on segment ab iff (am · ab) / |ab|² ∈ [0, 1] and the
            # perpendicular distance is zero.
            t = sum(am[k] * ab[k] for k in range(3)) / (ab_len * ab_len)
            perp = tuple(am[k] - t * ab[k] for k in range(3))
            assert math.sqrt(sum(c * c for c in perp)) < 1e-9, (
                f"rim midpoint {m} off chord {a}→{b}")
            assert -1e-9 <= t <= 1 + 1e-9


def test_corner_vertices_interpolated():
    # The six P_i are each endpoints of 2 sharp rim edges → held fixed by
    # the corner rule. Their positions must be exactly preserved across any
    # number of passes.
    verts, faces, sharp = _hex_control_mesh(corner_levels=(2, 0, 4, 1, 3, 5))
    out_verts, _, _ = subdivide_loop(verts, faces, sharp, 3)
    for i in range(6):
        original = verts[1 + i]   # P_i lives at index 1 + i in the input
        produced = out_verts[1 + i]
        assert produced == pytest.approx(original, abs=1e-12)


def test_smoothness_passes_zero_returns_control_mesh():
    # subdivide_loop with levels=0 must return the input untouched (verts
    # tuple-ified, faces unchanged), and one chain per sharp edge.
    verts, faces, sharp = _hex_control_mesh(corner_levels=(0, 1, 0, 1, 0, 1))
    out_verts, out_faces, rim_chains = subdivide_loop(verts, faces, sharp, 0)
    assert len(out_verts) == 13
    assert len(out_faces) == 18
    for i, v in enumerate(verts):
        assert out_verts[i] == pytest.approx(v, abs=1e-12)
    assert out_faces == [tuple(f) for f in faces]
    assert rim_chains == [list(e) for e in sharp]


def test_resample_density_does_not_introduce_new_smoothing():
    # After Loop smoothing, an extra `resample_density` pass must lay every
    # new vertex on the chord midpoint of its parent edge — chord-interpolation,
    # not surface-interpolation. So each new vert equals (a+b)/2 of its parents.
    verts, faces, sharp = _hex_control_mesh(corner_levels=(0, 2, 4, 2, 0, 0))
    sm_verts, sm_faces, sm_chains = subdivide_loop(verts, faces, sharp, 2)
    rs_verts, rs_faces, rs_chains = linear_midpoint_subdivide(
        sm_verts, sm_faces, sm_chains, 1
    )
    # The first len(sm_verts) entries are unchanged from the smoothing pass.
    for i, v in enumerate(sm_verts):
        assert rs_verts[i] == pytest.approx(v, abs=1e-12)
    # Each face was split via 3 midpoints. Walk every face and verify the
    # three new midpoint verts equal the chord midpoints of the parent edges.
    # We rebuild the edge→midpoint map by reading the first child sub-triangle
    # of each parent face — its (a, m_ab, m_ca) layout puts m_ab opposite a's
    # second neighbour. Easier: just sweep every new vert and assert that its
    # position is an exact midpoint of two original-mesh positions.
    for vi in range(len(sm_verts), len(rs_verts)):
        new = rs_verts[vi]
        # The new vert came from some parent edge (a, b) in sm_verts. Find a
        # pair whose midpoint matches. This is O(V²) but V is small in tests.
        found = False
        for a in range(len(sm_verts)):
            for b in range(a + 1, len(sm_verts)):
                mid = (
                    0.5 * (sm_verts[a][0] + sm_verts[b][0]),
                    0.5 * (sm_verts[a][1] + sm_verts[b][1]),
                    0.5 * (sm_verts[a][2] + sm_verts[b][2]),
                )
                if all(abs(new[k] - mid[k]) < 1e-9 for k in range(3)):
                    found = True
                    break
            if found:
                break
        assert found, f"new vert {new} at index {vi} is not a chord midpoint"


# ---------------------------------------------------------------------------
# Tile-level sanity, exercising build_hex_tile end-to-end.

@pytest.mark.parametrize("smoothness_passes", [0, 1, 2, 3])
def test_flat_tile_is_perfectly_flat(smoothness_passes):
    # Levels all zero, no override → C and every P_i at z=base_thickness. The
    # Q ring averages to z=base too, and every Loop stencil over a constant
    # height field returns that constant. So every top vert must equal base.
    base = 10.0
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=base,
        corner_levels=(0,) * 6,
        center_level=None,
        smoothness_passes=smoothness_passes,
    )
    # Tab/hole interlock geometry sits in [0, 8.2] mm; filter to the top.
    top_zs = [v[2] for v in verts if v[2] >= base - 1e-9]
    assert len(top_zs) > 0
    for z in top_zs:
        assert z == pytest.approx(base, abs=1e-9)


def test_rim_matches_neighbour_tile():
    # Two adjacent tiles must agree on every vertex along their shared rim.
    # Because rim edges are sharp, every midpoint is a linear midpoint of the
    # rim endpoints, and rim endpoints are propagated by `on_corner_changed`
    # to be equal on both tiles. So the rim chains coincide exactly.
    #
    # Verify by walking each rim vertex of two tiles whose chosen rim edge
    # ought to match (in world coords).
    from map import neighbour_coord, tile_world_xy, NE

    diameter = 100.0
    smooth = 2
    # Tile A at (0, 0). Tile B is the NE neighbour; per SHARED_CORNERS its
    # P5 (index 4) coincides with A.P1 (index 0) and its P4 (index 3) with
    # A.P2 (index 1). So we must pick B's corner levels with B[4] = A[0]
    # and B[3] = A[1] for the shared-rim z-heights to actually agree —
    # otherwise this test asserts on two tiles whose seams disagree by
    # design and the test is meaningless.
    levels_a = (2, 5, 0, 3, 1, 4)
    levels_b = (3, 0, 4, levels_a[1], levels_a[0], 2)
    verts_a, _ = build_hex_tile(diameter, 5.0, 10.0, levels_a, None, smooth)
    verts_b_local, _ = build_hex_tile(diameter, 5.0, 10.0, levels_b,
                                       None, smooth)
    qb, rb = neighbour_coord(0, 0, NE)
    bx, by = tile_world_xy(qb, rb, diameter)

    # A's rim 0 runs P1_a → P2_a (i.e., corner index 0 → 1). B's rim 3 runs
    # P4_b → P5_b. These are the SAME edge in world coords: P2_a == P4_b and
    # P1_a == P5_b (SHARED_CORNERS confirms this for the NE neighbour).
    #
    # Just verify that the set of (x, y, z) on A's rim 0 matches the set on
    # B's rim 3 (after translating B into world coords). Order doesn't
    # matter because the rim chains run from opposite ends.
    R = diameter / 2.0

    def _corner_world_xy(i, dx=0.0, dy=0.0):
        a = math.pi / 3.0 - i * (math.pi / 3.0)
        return (R * math.cos(a) + dx, R * math.sin(a) + dy)

    # P1_a and P5_b are the same world point; P2_a and P4_b are the same.
    a_p1 = _corner_world_xy(0)
    a_p2 = _corner_world_xy(1)
    b_p4 = _corner_world_xy(3, bx, by)
    b_p5 = _corner_world_xy(4, bx, by)
    assert a_p1 == pytest.approx(b_p5, abs=1e-9)
    assert a_p2 == pytest.approx(b_p4, abs=1e-9)

    # Sample every vert ≥ base_thickness lying on each tile's shared rim
    # (the line segment between A.P1 and A.P2). Then compare the two sets.
    def _on_rim_a(v, base=10.0):
        if v[2] < base - 1e-9:
            return False
        ab = (a_p2[0] - a_p1[0], a_p2[1] - a_p1[1])
        ap = (v[0] - a_p1[0], v[1] - a_p1[1])
        ab2 = ab[0] * ab[0] + ab[1] * ab[1]
        t = (ap[0] * ab[0] + ap[1] * ab[1]) / ab2
        if t < -1e-6 or t > 1 + 1e-6:
            return False
        perp = (ap[0] - t * ab[0], ap[1] - t * ab[1])
        return math.hypot(*perp) < 1e-6

    a_rim = sorted(
        (round(v[0], 6), round(v[1], 6), round(v[2], 6))
        for v in verts_a if _on_rim_a(v)
    )
    b_world = [(v[0] + bx, v[1] + by, v[2]) for v in verts_b_local]
    b_rim = sorted(
        (round(v[0], 6), round(v[1], 6), round(v[2], 6))
        for v in b_world if _on_rim_a(v)
    )
    assert a_rim == b_rim


# ---------------------------------------------------------------------------
# Loop stencil sanity.

def test_beta_at_known_valences():
    # Spot-check the closed form against published values.
    assert _beta(3) == pytest.approx(3.0 / 16.0, abs=1e-12)
    # n=6: (1/6)·(5/8 − (3/8 + 1/4·cos(60°))²) = (1/6)·(5/8 − 1/4) = 1/16.
    assert _beta(6) == pytest.approx(1.0 / 16.0, abs=1e-9)
