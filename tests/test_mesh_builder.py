import math
import pytest

from mesh_builder import (
    build_hex_tile,
    clamp_center_to_hexagon,
    _hermite_basis,
    _patch_data,
    _eval_coons,
)
from manifold_check import assert_two_manifold


# ---------------------------------------------------------------------------
# Coons-patch helpers shared by the new Hermite/patch tests below.
# Mirror what build_hex_tile constructs internally so tests can call
# _patch_data / _eval_coons directly with the same conventions.

def _make_patches(corner_levels=(0, 1, 2, 1, 0, 0),
                  center_level=None,
                  diameter_mm=100.0,
                  level_height_mm=5.0,
                  base_thickness_mm=3.0,
                  center_xy=(0.0, 0.0)):
    """Build the six per-patch data dicts the way build_hex_tile does."""
    R = diameter_mm / 2.0
    apothem = R * math.sqrt(3.0) / 2.0
    levels = tuple(max(0, int(L)) for L in corner_levels)
    corners_xy = []
    for i in range(6):
        angle = math.pi / 2.0 - i * (math.pi / 3.0)
        corners_xy.append((R * math.cos(angle), R * math.sin(angle)))
    corner_z = [base_thickness_mm + levels[i] * level_height_mm for i in range(6)]
    if center_level is not None:
        center_z = base_thickness_mm + max(0, int(center_level)) * level_height_mm
    else:
        center_z = sum(corner_z) / 6.0
    C_pos = (float(center_xy[0]), float(center_xy[1]), center_z)
    corner_pos = [(corners_xy[i][0], corners_xy[i][1], corner_z[i]) for i in range(6)]
    spoke_normals = []
    for i in range(6):
        ux = corner_pos[i][0] - C_pos[0]
        uy = corner_pos[i][1] - C_pos[1]
        mag = math.hypot(ux, uy)
        spoke_normals.append((-uy / mag, ux / mag, 0.0))
    rim_normals = []
    for i in range(6):
        ip1 = (i + 1) % 6
        mx = 0.5 * (corner_pos[i][0] + corner_pos[ip1][0])
        my = 0.5 * (corner_pos[i][1] + corner_pos[ip1][1])
        mag = math.hypot(mx, my)
        rim_normals.append((mx / mag, my / mag, 0.0))
    alpha = 1.0
    beta = apothem
    gamma = apothem
    return [
        _patch_data(i, C_pos, corner_pos, spoke_normals, rim_normals,
                    alpha, beta, gamma)
        for i in range(6)
    ], C_pos, corner_pos


def _build(
    subdivisions=4,
    corner_levels=(0, 1, 2, 1, 0, 0),
    center_level=None,
    diameter_mm=100.0,
    level_height_mm=5.0,
    base_thickness_mm=3.0,
):
    return build_hex_tile(
        diameter_mm=diameter_mm,
        level_height_mm=level_height_mm,
        base_thickness_mm=base_thickness_mm,
        corner_levels=corner_levels,
        center_level=center_level,
        subdivisions=subdivisions,
    )


@pytest.mark.parametrize(
    "subdivisions,expected_v",
    [
        (0, 14),   # N=1: top 6N²+1=7   + bottom 7
        (1, 38),   # N=2: top 6N²+1=25  + bottom 13
        (2, 74),   # N=3: top 6N²+1=55  + bottom 19
        (4, 182),  # N=5: top 6N²+1=151 + bottom 31
    ],
)
def test_vertex_count(subdivisions, expected_v):
    verts, _ = _build(subdivisions=subdivisions)
    assert len(verts) == expected_v


@pytest.mark.parametrize(
    "subdivisions,expected_f",
    [
        (0, 18),   # N=1: 6 top + 6 side + 6 bottom
        (1, 48),   # N=2: 24 top + 12 side + 12 bottom
        (2, 90),
        (4, 210),
    ],
)
def test_face_count(subdivisions, expected_f):
    _, faces = _build(subdivisions=subdivisions)
    assert len(faces) == expected_f


@pytest.mark.parametrize("subdivisions", [0, 1, 2, 4])
@pytest.mark.parametrize(
    "corner_levels",
    [
        (0, 0, 0, 0, 0, 0),
        (0, 1, 2, 1, 0, 0),
        (3, 3, 3, 3, 3, 3),
        (5, 0, 5, 0, 5, 0),
        (0, 0, 0, 0, 0, 7),
    ],
)
def test_manifold(subdivisions, corner_levels):
    verts, faces = _build(subdivisions=subdivisions, corner_levels=corner_levels)
    assert_two_manifold(verts, faces)


def test_manifold_with_center_override():
    verts, faces = _build(corner_levels=(0, 2, 4, 2, 0, 0), center_level=6)
    assert_two_manifold(verts, faces)


def test_euler_characteristic_is_sphere():
    # Closed 2-manifold homeomorphic to a sphere: V - E + F = 2.
    verts, faces = _build(subdivisions=3, corner_levels=(0, 1, 2, 1, 0, 0))
    edges = set()
    for face in faces:
        n = len(face)
        for k in range(n):
            a, b = face[k], face[(k + 1) % n]
            edges.add((a, b) if a < b else (b, a))
    assert len(verts) - len(edges) + len(faces) == 2


def test_corner_z_formula():
    base, lh = 3.0, 5.0
    levels = (0, 1, 2, 3, 4, 5)
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=lh,
        base_thickness_mm=base,
        corner_levels=levels,
        center_level=None,
        subdivisions=0,
    )
    R_m = 50.0 / 1000.0
    for i in range(6):
        angle = math.pi / 2.0 - i * (math.pi / 3.0)
        cx, cy = R_m * math.cos(angle), R_m * math.sin(angle)
        # Top vertex at this XY has Z > 0; bottom vertex shares XY but Z=0.
        top_matches = [
            v for v in verts
            if abs(v[0] - cx) < 1e-9 and abs(v[1] - cy) < 1e-9 and v[2] > 1e-9
        ]
        assert len(top_matches) == 1, f"corner {i+1}: expected one top vertex"
        expected_z = (base + levels[i] * lh) / 1000.0
        assert top_matches[0][2] == pytest.approx(expected_z, abs=1e-12)


def test_center_override_pins_center_vertex():
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=3.0,
        corner_levels=(0, 0, 0, 0, 0, 0),
        center_level=5,
        subdivisions=0,
    )
    top_center = [
        v for v in verts
        if abs(v[0]) < 1e-12 and abs(v[1]) < 1e-12 and v[2] > 1e-9
    ]
    assert len(top_center) == 1
    assert top_center[0][2] == pytest.approx(28.0 / 1000.0, abs=1e-12)


def test_center_without_override_uses_corner_mean():
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=3.0,
        corner_levels=(0, 6, 0, 6, 0, 6),
        center_level=None,
        subdivisions=0,
    )
    top_center = [
        v for v in verts
        if abs(v[0]) < 1e-12 and abs(v[1]) < 1e-12 and v[2] > 1e-9
    ]
    assert len(top_center) == 1
    # Mean of (3, 33, 3, 33, 3, 33) mm = 18 mm = 0.018 m.
    assert top_center[0][2] == pytest.approx(18.0 / 1000.0, abs=1e-12)


def test_bottom_is_flat_at_zero():
    verts, _ = _build()
    bottom_verts = [v for v in verts if v[2] < 1e-12]
    assert len(bottom_verts) > 0
    for v in bottom_verts:
        assert v[2] == 0.0


def test_clamps_negative_corner_levels():
    neg_verts, _ = build_hex_tile(100.0, 5.0, 3.0, (-2, -1, 0, 1, 2, 3), None, 0)
    zero_verts, _ = build_hex_tile(100.0, 5.0, 3.0, (0, 0, 0, 1, 2, 3), None, 0)
    assert sorted(v[2] for v in neg_verts) == pytest.approx(
        sorted(v[2] for v in zero_verts)
    )


def test_validates_inputs():
    with pytest.raises(ValueError):
        build_hex_tile(0.0, 5.0, 3.0, (0,) * 6, None, 0)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 0.0, 3.0, (0,) * 6, None, 0)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 0.0, (0,) * 6, None, 0)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 3.0, (0,) * 6, None, -1)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 3.0, (0,) * 5, None, 0)


def test_units_convert_mm_to_meters():
    # Diameter 100 mm should put corners 0.05 m from origin.
    verts, _ = build_hex_tile(100.0, 5.0, 3.0, (0,) * 6, None, 0)
    max_radius = max(math.hypot(v[0], v[1]) for v in verts)
    assert max_radius == pytest.approx(0.05, abs=1e-9)


# ---------------------------------------------------------------------------
# Helper-level Hermite basis tests.

def test_hermite_basis_endpoints():
    # H0/H1 interpolate values; G0/G1 vanish at both ends so tangents do not
    # leak into corner positions.
    assert _hermite_basis(0.0) == pytest.approx((1.0, 0.0, 0.0, 0.0), abs=1e-12)
    assert _hermite_basis(1.0) == pytest.approx((0.0, 1.0, 0.0, 0.0), abs=1e-12)


def test_hermite_basis_derivatives():
    # Numerical derivative at t=0 → (0, 0, 1, 0); at t=1 → (0, 0, 0, 1).
    eps = 1e-6
    d0 = tuple((b - a) / eps
               for a, b in zip(_hermite_basis(0.0), _hermite_basis(eps)))
    d1 = tuple((b - a) / eps
               for a, b in zip(_hermite_basis(1.0 - eps), _hermite_basis(1.0)))
    assert d0 == pytest.approx((0.0, 0.0, 1.0, 0.0), abs=1e-5)
    assert d1 == pytest.approx((0.0, 0.0, 0.0, 1.0), abs=1e-5)


# ---------------------------------------------------------------------------
# Per-patch G0 tests: positions on the four boundaries are exact.

def test_patch_apex_is_C():
    patches, C_pos, _ = _make_patches()
    for i, pd in enumerate(patches):
        for v in (0.0, 0.25, 0.5, 0.75, 1.0):
            p = _eval_coons(0.0, v, pd)
            assert p == pytest.approx(C_pos, abs=1e-12), (
                f"patch {i} at u=0, v={v}: expected apex C, got {p}")


def test_patch_corners_match_Pi():
    patches, _, corner_pos = _make_patches()
    for i, pd in enumerate(patches):
        ip1 = (i + 1) % 6
        assert _eval_coons(1.0, 0.0, pd) == pytest.approx(corner_pos[i], abs=1e-12)
        assert _eval_coons(1.0, 1.0, pd) == pytest.approx(corner_pos[ip1], abs=1e-12)


def test_patch_rim_is_straight_line():
    patches, _, corner_pos = _make_patches()
    for i, pd in enumerate(patches):
        ip1 = (i + 1) % 6
        Pi, Pip1 = corner_pos[i], corner_pos[ip1]
        for v in (0.1, 0.3, 0.5, 0.7, 0.9):
            expected = tuple((1.0 - v) * Pi[k] + v * Pip1[k] for k in range(3))
            assert _eval_coons(1.0, v, pd) == pytest.approx(expected, abs=1e-12)


def test_patch_spokes_are_straight_lines():
    patches, C_pos, corner_pos = _make_patches()
    for i, pd in enumerate(patches):
        ip1 = (i + 1) % 6
        Pi, Pip1 = corner_pos[i], corner_pos[ip1]
        for u in (0.1, 0.3, 0.5, 0.7, 0.9):
            exp_v0 = tuple((1.0 - u) * C_pos[k] + u * Pi[k] for k in range(3))
            exp_v1 = tuple((1.0 - u) * C_pos[k] + u * Pip1[k] for k in range(3))
            assert _eval_coons(u, 0.0, pd) == pytest.approx(exp_v0, abs=1e-12)
            assert _eval_coons(u, 1.0, pd) == pytest.approx(exp_v1, abs=1e-12)


# ---------------------------------------------------------------------------
# Bicubic blend isolation: F_u + F_v - B must cancel correctly on boundaries
# and reduce to a Hermite product near a singly-active corner.

def test_blend_term_cancels_on_boundary():
    # Synthetic patch with zero corner positions and only non-zero tangents.
    # Since G0/G1 vanish at both endpoints, evaluating at the four corners
    # of (u,v) parameter space should give the corner positions exactly =
    # (0,0,0). If F_u + F_v - B does NOT collapse correctly, tangents will
    # bleed into corner outputs.
    pd = {
        "C":         (0.0, 0.0, 0.0),
        "Pi":        (0.0, 0.0, 0.0),
        "Pip1":      (0.0, 0.0, 0.0),
        "Tu0_at_v0": (1.0, 2.0, 3.0),
        "Tu0_at_v1": (4.0, 5.0, 6.0),
        "Tu1":       (7.0, 8.0, 9.0),
        "Tv0":       (10.0, 11.0, 12.0),
        "Tv1":       (13.0, 14.0, 15.0),
    }
    zero = (0.0, 0.0, 0.0)
    for (u, v) in [(0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)]:
        assert _eval_coons(u, v, pd) == pytest.approx(zero, abs=1e-12)


# ---------------------------------------------------------------------------
# Continuity tests on the evaluated surface.
#
# The bilinearly blended Coons patch achieves G0 continuity at every internal
# spoke and along the rim (positions match exactly), and is C∞ inside each
# patch. Strict G1 across the spokes is NOT delivered: the actual ∂S/∂v at
# the spoke depends on each patch's own rim curve, which differs between
# neighbours. The tests below pin down the continuity properties that DO
# hold; documenting the visible smoothness is the README's job.

def _patch_partial_u(u, v, pd, eps=1e-5):
    a = _eval_coons(u - eps, v, pd)
    b = _eval_coons(u + eps, v, pd)
    return tuple((b[k] - a[k]) / (2 * eps) for k in range(3))


def _patch_partial_v(u, v, pd, eps=1e-5):
    a = _eval_coons(u, v - eps, pd)
    b = _eval_coons(u, v + eps, pd)
    return tuple((b[k] - a[k]) / (2 * eps) for k in range(3))


@pytest.mark.parametrize("center_xy", [(0.0, 0.0), (5.0, 3.0), (-8.0, 2.0)])
def test_spoke_position_G0_across_patches(center_xy):
    # G0 continuity across every internal spoke: patch i evaluated at v=0
    # and patch (i-1) at v=1 produce the same point at every u along the
    # shared spoke. This is the precondition for vertex dedup to be safe.
    # Must hold for off-center C too — the spokes are shared by construction.
    patches, _, _ = _make_patches(corner_levels=(0, 3, 0, 3, 0, 3),
                                  center_xy=center_xy)
    for spoke_i in range(6):
        pd_left = patches[(spoke_i - 1) % 6]
        pd_right = patches[spoke_i]
        for u in (0.0, 0.1, 0.37, 0.5, 0.73, 0.9, 1.0):
            p_left = _eval_coons(u, 1.0, pd_left)
            p_right = _eval_coons(u, 0.0, pd_right)
            assert p_left == pytest.approx(p_right, abs=1e-12)


@pytest.mark.parametrize("center_xy", [(0.0, 0.0), (5.0, 3.0), (-8.0, 2.0)])
def test_spoke_direction_derivative_matches_across_patches(center_xy):
    # The u-direction (along the spoke) derivative IS shared between
    # neighbouring patches — both sides give Pi - C (the straight spoke).
    # This is a partial G1 property that the construction does deliver.
    patches, C_pos, corner_pos = _make_patches(corner_levels=(5, 0, 5, 0, 5, 0),
                                               center_xy=center_xy)
    eps = 1e-5
    for spoke_i in range(6):
        pd_left = patches[(spoke_i - 1) % 6]
        pd_right = patches[spoke_i]
        expected = tuple(corner_pos[spoke_i][k] - C_pos[k] for k in range(3))
        for u in (0.2, 0.5, 0.8):
            du_left = _patch_partial_u(u, 1.0, pd_left, eps=eps)
            du_right = _patch_partial_u(u, 0.0, pd_right, eps=eps)
            assert du_left == pytest.approx(expected, abs=1e-4)
            assert du_right == pytest.approx(expected, abs=1e-4)


def test_rim_position_matches_neighbour_tile():
    # G0 across a tile-to-tile rim seam: the rim curve is the same straight
    # line on both tiles. The cross-rim derivative magnitude differs across
    # the seam (each tile has its own center), so the join is G0 but not
    # G1 — visible-but-acceptable under shade-smooth, which is what the
    # README documents.
    p_a, _, _ = _make_patches(corner_levels=(0, 1, 2, 1, 0, 3))
    p_b, _, _ = _make_patches(corner_levels=(0, 1, 5, 4, 0, 0))
    pd_a, pd_b = p_a[0], p_b[0]  # both tiles share rim 0
    for v in (0.0, 0.1, 0.5, 0.9, 1.0):
        assert _eval_coons(1.0, v, pd_a) == pytest.approx(
            _eval_coons(1.0, v, pd_b), abs=1e-12)


def test_rim_cross_tangent_formula():
    # Document the actual cross-rim derivative: ∂S/∂u(1, v) =
    # H0(v)·(Pi − C) + H1(v)·(Pip1 − C). The assigned Tu1 cancels against
    # the corresponding B row, leaving only the F_v contribution.
    patches, C_pos, corner_pos = _make_patches(corner_levels=(0, 2, 4, 1, 0, 3))
    eps = 1e-5
    for i, pd in enumerate(patches):
        ip1 = (i + 1) % 6
        for v in (0.1, 0.3, 0.5, 0.7, 0.9):
            H0v, H1v, _, _ = _hermite_basis(v)
            expected = tuple(
                H0v * (corner_pos[i][k] - C_pos[k])
                + H1v * (corner_pos[ip1][k] - C_pos[k])
                for k in range(3))
            # Backward FD because u=1 is a boundary.
            a = _eval_coons(1.0 - eps, v, pd)
            b = _eval_coons(1.0, v, pd)
            actual = tuple((b[k] - a[k]) / eps for k in range(3))
            assert actual == pytest.approx(expected, abs=1e-3)


def test_patch_interior_is_smooth():
    # Sanity: the surface is C∞ inside a single patch (no parameter-space
    # discontinuities). Verify by sampling a dense grid and confirming that
    # consecutive partial derivatives in u and v vary smoothly (no jumps
    # larger than what a smooth function should produce).
    patches, _, _ = _make_patches(corner_levels=(0, 2, 4, 1, 0, 3))
    pd = patches[0]
    eps = 1e-4
    # Compare ∂S/∂u at adjacent v values: difference should be O(Δv).
    prev = None
    for v in [0.1 + 0.05 * k for k in range(15)]:
        du = _patch_partial_u(0.5, v, pd, eps=eps)
        if prev is not None:
            diff = max(abs(du[k] - prev[k]) for k in range(3))
            # Δv = 0.05; a smooth derivative should not change by more than
            # a few hex-diameter units per Δv unit.
            assert diff < 100.0, f"v={v}: ∂S/∂u jump = {diff}"
        prev = du


# ---------------------------------------------------------------------------
# Flat-tile invariant: every blend / sign error eventually shows up here.

def test_flat_tile_is_perfectly_flat():
    # Levels all zero, no override → C_pos.z = base_thickness, all corners
    # at the same z. The Coons patch must collapse to a horizontal disk at
    # z = base / 1000 metres, including for non-grid-aligned (u, v).
    base = 3.0
    verts, _ = build_hex_tile(100.0, 5.0, base, (0,) * 6, None, 2)
    top_z_values = [v[2] for v in verts if v[2] > 1e-9]
    expected = base / 1000.0
    for z in top_z_values:
        assert z == pytest.approx(expected, abs=1e-12)
    # Now also sample the surface at dense non-grid (u, v) points.
    patches, _, _ = _make_patches(corner_levels=(0,) * 6,
                                  base_thickness_mm=base)
    for pd in patches:
        for u in (0.13, 0.27, 0.41, 0.58, 0.69, 0.83):
            for v in (0.07, 0.31, 0.52, 0.74, 0.91):
                p = _eval_coons(u, v, pd)
                # In mm space (patch space): z should equal base.
                assert p[2] == pytest.approx(base, abs=1e-9)


# ---------------------------------------------------------------------------
# Vertex-dedup: G1-on-spokes guarantee depends on shared keys.

def test_spoke_vertices_are_deduplicated():
    # Top-vertex formula 6N² + 1. Verify by counting vertices above the
    # bottom (z > 0).
    for subdivisions in (0, 1, 2, 4):
        N = subdivisions + 1
        verts, _ = _build(subdivisions=subdivisions, corner_levels=(1,) * 6)
        top_count = sum(1 for v in verts if v[2] > 1e-9)
        assert top_count == 6 * N * N + 1, (
            f"sub={subdivisions} N={N}: top vertex count {top_count} != "
            f"expected {6 * N * N + 1}")

    # Additionally verify that interior spoke samples on adjacent patches
    # collide exactly (same XY and Z), which they would only if the dedup
    # map merged them under the same key.
    patches, _, _ = _make_patches(corner_levels=(0, 3, 0, 3, 0, 3))
    for spoke_i in range(6):
        pd_left = patches[(spoke_i - 1) % 6]
        pd_right = patches[spoke_i]
        for u in (0.2, 0.5, 0.8):
            p_left = _eval_coons(u, 1.0, pd_left)
            p_right = _eval_coons(u, 0.0, pd_right)
            assert p_left == pytest.approx(p_right, abs=1e-12)


# ---------------------------------------------------------------------------
# Center XY: gizmo-driven drag of the apex in the XY plane.

def test_clamp_center_to_hexagon_interior_passes_through():
    # Points well inside the hexagon must not be modified.
    for (x, y) in [(0.0, 0.0), (5.0, 3.0), (-10.0, -2.0), (15.0, 0.0)]:
        cx, cy = clamp_center_to_hexagon(x, y, diameter_mm=100.0)
        assert cx == pytest.approx(x, abs=1e-12)
        assert cy == pytest.approx(y, abs=1e-12)


def test_clamp_center_to_hexagon_rim_midpoints():
    # Push a point 5 mm past each rim midpoint along the outward normal.
    # Each must clamp to a point at distance (apothem - 1) along the same
    # normal — i.e., the clamp lands the point inside the hex by the
    # configured 1 mm safety buffer.
    diameter = 100.0
    apothem = (diameter / 2.0) * math.sqrt(3.0) / 2.0
    limit = apothem - 1.0  # default safety_mm=1.0
    for i in range(6):
        theta = math.pi / 3.0 - i * (math.pi / 3.0)
        nx, ny = math.cos(theta), math.sin(theta)
        # 5 mm outside the rim along the outward normal.
        x = (apothem + 5.0) * nx
        y = (apothem + 5.0) * ny
        cx, cy = clamp_center_to_hexagon(x, y, diameter_mm=diameter)
        # Should sit exactly on the half-plane line at limit·n_i.
        assert cx == pytest.approx(limit * nx, abs=1e-9)
        assert cy == pytest.approx(limit * ny, abs=1e-9)


def test_clamp_center_to_hexagon_far_outside_lands_inside():
    # An aggressively out-of-range point must end up strictly inside the
    # safety-margined hexagon along every rim normal.
    diameter = 100.0
    apothem = (diameter / 2.0) * math.sqrt(3.0) / 2.0
    limit = apothem - 1.0
    cx, cy = clamp_center_to_hexagon(1000.0, 1000.0, diameter_mm=diameter)
    for i in range(6):
        theta = math.pi / 3.0 - i * (math.pi / 3.0)
        nx, ny = math.cos(theta), math.sin(theta)
        d = nx * cx + ny * cy
        assert d <= limit + 1e-9


def test_clamp_idempotent():
    # Clamping an already-clamped value must not drift — important because
    # the gizmo writes the clamped XY back into the property, which then
    # triggers the update callback which clamps again.
    diameter = 100.0
    for (x, y) in [(50.0, 50.0), (-200.0, 30.0), (10.0, -150.0), (0.5, 0.5)]:
        once = clamp_center_to_hexagon(x, y, diameter_mm=diameter)
        twice = clamp_center_to_hexagon(*once, diameter_mm=diameter)
        assert twice[0] == pytest.approx(once[0], abs=1e-12)
        assert twice[1] == pytest.approx(once[1], abs=1e-12)


def test_build_hex_tile_accepts_center_xy():
    # With all corners at level 0 and override pinning the center high, the
    # apex is the unique highest top vertex. Its XY must equal the requested
    # center_xy (converted to metres).
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=3.0,
        corner_levels=(0,) * 6,
        center_level=4,
        subdivisions=2,
        center_xy=(5.0, -3.0),
    )
    apex = max(verts, key=lambda v: v[2])
    assert apex[0] == pytest.approx(0.005, abs=1e-9)
    assert apex[1] == pytest.approx(-0.003, abs=1e-9)


def test_flat_tile_off_center():
    # Off-center C still produces a perfectly flat top when corner levels
    # are uniform and there is no override (center_z == base_thickness).
    base = 3.0
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=base,
        corner_levels=(0,) * 6,
        center_level=None,
        subdivisions=2,
        center_xy=(7.0, -4.0),
    )
    expected = base / 1000.0
    for v in verts:
        if v[2] > 1e-9:
            assert v[2] == pytest.approx(expected, abs=1e-12)
