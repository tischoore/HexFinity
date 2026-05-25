import math
import pytest

from mesh_builder import (
    build_hex_tile,
    clamp_center_to_hexagon,
    _hermite_basis,
    _patch_data,
    _eval_coons,
    TAB_WIDTH_MM,
    TAB_HEIGHT_MM,
    TAB_DEPTH_MM,
    TAB_OFFSET_FROM_CORNER_MM,
    TAB_HOLE_TOLERANCE_MM,
)
from manifold_check import assert_two_manifold
from map import neighbour_coord, tile_world_xy, NE


# ---------------------------------------------------------------------------
# Coons-patch helpers shared by the new Hermite/patch tests below.
# Mirror what build_hex_tile constructs internally so tests can call
# _patch_data / _eval_coons directly with the same conventions.

def _make_patches(corner_levels=(0, 1, 2, 1, 0, 0),
                  center_level=None,
                  diameter_mm=100.0,
                  level_height_mm=5.0,
                  base_thickness_mm=10.0,
                  center_xy=(0.0, 0.0)):
    """Build the six per-patch data dicts the way build_hex_tile does."""
    R = diameter_mm / 2.0
    apothem = R * math.sqrt(3.0) / 2.0
    levels = tuple(max(0, int(L)) for L in corner_levels)
    corners_xy = []
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
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
    base_thickness_mm=10.0,
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
        # 6N² + 1 top + 103 bottom-region (6 bcorners + 24 bbreaks + 1 bcenter
        # + 36 tab + 36 hole verts; tab/hole share 2 bbreak verts each per side
        # so each contributes 6 new verts × 6 sides).
        (0, 110),  # N=1: 7   + 103
        (1, 128),  # N=2: 25  + 103
        (2, 158),  # N=3: 55  + 103
        (4, 254),  # N=5: 151 + 103
    ],
)
def test_vertex_count(subdivisions, expected_v):
    verts, _ = _build(subdivisions=subdivisions)
    assert len(verts) == expected_v


@pytest.mark.parametrize(
    "subdivisions,expected_f",
    [
        # 6N² top + 6 side-wall n-gons + 30 tab faces (5/side) + 24 cavity
        # faces (4/side) + 42 bottom-plate fan triangles (7/side).
        (0, 108),
        (1, 126),
        (2, 156),
        (4, 252),
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
    base, lh = 10.0, 5.0
    levels = (0, 1, 2, 3, 4, 5)
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=lh,
        base_thickness_mm=base,
        corner_levels=levels,
        center_level=None,
        subdivisions=0,
    )
    R = 50.0
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        cx, cy = R * math.cos(angle), R * math.sin(angle)
        # Top vertex at this XY has Z > 0; bottom vertex shares XY but Z=0.
        top_matches = [
            v for v in verts
            if abs(v[0] - cx) < 1e-6 and abs(v[1] - cy) < 1e-6 and v[2] > 1e-6
        ]
        assert len(top_matches) == 1, f"corner {i+1}: expected one top vertex"
        expected_z = base + levels[i] * lh
        assert top_matches[0][2] == pytest.approx(expected_z, abs=1e-9)


def test_center_override_pins_center_vertex():
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=10.0,
        corner_levels=(0, 0, 0, 0, 0, 0),
        center_level=5,
        subdivisions=0,
    )
    top_center = [
        v for v in verts
        if abs(v[0]) < 1e-9 and abs(v[1]) < 1e-9 and v[2] > 1e-6
    ]
    assert len(top_center) == 1
    assert top_center[0][2] == pytest.approx(35.0, abs=1e-9)


def test_center_without_override_uses_corner_mean():
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=10.0,
        corner_levels=(0, 6, 0, 6, 0, 6),
        center_level=None,
        subdivisions=0,
    )
    top_center = [
        v for v in verts
        if abs(v[0]) < 1e-9 and abs(v[1]) < 1e-9 and v[2] > 1e-6
    ]
    assert len(top_center) == 1
    # Mean of (10, 40, 10, 40, 10, 40) mm = 25 mm.
    assert top_center[0][2] == pytest.approx(25.0, abs=1e-9)


def test_bottom_is_flat_at_zero():
    verts, _ = _build()
    bottom_verts = [v for v in verts if v[2] < 1e-12]
    assert len(bottom_verts) > 0
    for v in bottom_verts:
        assert v[2] == 0.0


def test_clamps_negative_corner_levels():
    neg_verts, _ = build_hex_tile(100.0, 5.0, 10.0, (-2, -1, 0, 1, 2, 3), None, 0)
    zero_verts, _ = build_hex_tile(100.0, 5.0, 10.0, (0, 0, 0, 1, 2, 3), None, 0)
    assert sorted(v[2] for v in neg_verts) == pytest.approx(
        sorted(v[2] for v in zero_verts)
    )


def test_validates_inputs():
    with pytest.raises(ValueError):
        build_hex_tile(0.0, 5.0, 10.0, (0,) * 6, None, 0)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 0.0, 10.0, (0,) * 6, None, 0)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 0.0, (0,) * 6, None, 0)
    # Tab/hole interlock needs base_thickness_mm >= TAB_HEIGHT + TOL (8.2 mm).
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 3.0, (0,) * 6, None, 0)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 10.0, (0,) * 6, None, -1)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 10.0, (0,) * 5, None, 0)
    # Diameter must also be wide enough for the tab and hole on a single side
    # to leave a printable gap of solid material between them.
    with pytest.raises(ValueError):
        build_hex_tile(60.0, 5.0, 10.0, (0,) * 6, None, 0)


def test_units_are_millimetres():
    # Diameter 100 mm should put hex corners 50 mm from origin (mesh is in mm
    # so STL export at default settings writes correct physical size). Tabs
    # extend further out radially, so check the hex corners explicitly.
    verts, _ = build_hex_tile(100.0, 5.0, 10.0, (0,) * 6, None, 0)
    R = 50.0
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        cx, cy = R * math.cos(angle), R * math.sin(angle)
        matches = [v for v in verts
                   if abs(v[0] - cx) < 1e-9 and abs(v[1] - cy) < 1e-9]
        assert matches, f"corner {i+1} at ({cx:.3f},{cy:.3f}) not found"


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
    # z = base millimetres, including for non-grid-aligned (u, v).
    base = 10.0
    verts, _ = build_hex_tile(100.0, 5.0, base, (0,) * 6, None, 2)
    # Tab/hole interlock verts live in the lower 8.1 mm of the base; filter
    # them out so we only check the Coons-patch top surface.
    top_z_values = [v[2] for v in verts if v[2] >= base - 1e-9]
    expected = base
    for z in top_z_values:
        assert z == pytest.approx(expected, abs=1e-9)
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
    # Top-vertex formula 6N² + 1. Verify by counting vertices at or above the
    # base thickness (10 mm), which excludes the tab/hole interlock geometry
    # that sits in the lower 8.1 mm of the base.
    for subdivisions in (0, 1, 2, 4):
        N = subdivisions + 1
        verts, _ = _build(subdivisions=subdivisions, corner_levels=(1,) * 6)
        top_count = sum(1 for v in verts if v[2] >= 10.0 - 1e-9)
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
        theta = math.pi / 6.0 - i * (math.pi / 3.0)
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
        theta = math.pi / 6.0 - i * (math.pi / 3.0)
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
    # center_xy in millimetres.
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=10.0,
        corner_levels=(0,) * 6,
        center_level=4,
        subdivisions=2,
        center_xy=(5.0, -3.0),
    )
    apex = max(verts, key=lambda v: v[2])
    assert apex[0] == pytest.approx(5.0, abs=1e-9)
    assert apex[1] == pytest.approx(-3.0, abs=1e-9)


def test_flat_tile_off_center():
    # Off-center C still produces a perfectly flat top when corner levels
    # are uniform and there is no override (center_z == base_thickness).
    base = 10.0
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=base,
        corner_levels=(0,) * 6,
        center_level=None,
        subdivisions=2,
        center_xy=(7.0, -4.0),
    )
    expected = base
    for v in verts:
        if v[2] >= base - 1e-9:
            assert v[2] == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Inter-tile tab/hole interlock — geometric placement + neighbour mating.

def _side0_frame(diameter=100.0):
    """Return (P1, P2, side_len, outward) for side 0 in tile-local coords."""
    R = diameter / 2.0
    P1 = (R * math.cos(math.pi / 3.0), R * math.sin(math.pi / 3.0))
    P2 = (R, 0.0)
    mid = ((P1[0] + P2[0]) / 2.0, (P1[1] + P2[1]) / 2.0)
    mag = math.hypot(*mid)
    outward = (mid[0] / mag, mid[1] / mag)
    return P1, P2, R, outward


def _pos_along_side(P_lo, P_hi, side_len, outward, u, radial, z):
    rim_x = P_lo[0] + (u / side_len) * (P_hi[0] - P_lo[0])
    rim_y = P_lo[1] + (u / side_len) * (P_hi[1] - P_lo[1])
    return (rim_x + radial * outward[0],
            rim_y + radial * outward[1],
            z)


def _find_vertex(verts, target, tol=1e-6):
    for v in verts:
        if (abs(v[0] - target[0]) < tol and
                abs(v[1] - target[1]) < tol and
                abs(v[2] - target[2]) < tol):
            return v
    return None


def test_tab_corners_placed_on_side_0():
    # Side 0 tab is 10 mm from P2 (the clockwise-next corner of side 0). All
    # 8 tab corners must exist in the mesh at the spec-derived positions.
    P1, P2, side_len, outward = _side0_frame()
    u_lo = side_len - TAB_OFFSET_FROM_CORNER_MM - TAB_WIDTH_MM
    u_hi = side_len - TAB_OFFSET_FROM_CORNER_MM
    verts, _ = build_hex_tile(100.0, 5.0, 10.0, (0,) * 6, None, 0)
    for u in (u_lo, u_hi):
        for radial in (0.0, TAB_DEPTH_MM):
            for z in (0.0, TAB_HEIGHT_MM):
                target = _pos_along_side(P1, P2, side_len, outward, u, radial, z)
                assert _find_vertex(verts, target) is not None, (
                    f"no tab vertex at {target}")


def test_hole_corners_placed_on_side_0():
    # Side 0 hole is 10 mm from P1 (the clockwise-previous corner) and TOL/2
    # wider on each side. Inner cavity corners sit `hole_depth` mm into the
    # hex along -outward.
    P1, P2, side_len, outward = _side0_frame()
    u_lo = TAB_OFFSET_FROM_CORNER_MM - TAB_HOLE_TOLERANCE_MM / 2.0
    u_hi = TAB_OFFSET_FROM_CORNER_MM + TAB_WIDTH_MM + TAB_HOLE_TOLERANCE_MM / 2.0
    hole_depth = TAB_DEPTH_MM + TAB_HOLE_TOLERANCE_MM
    hole_top = TAB_HEIGHT_MM + TAB_HOLE_TOLERANCE_MM
    verts, _ = build_hex_tile(100.0, 5.0, 10.0, (0,) * 6, None, 0)
    for u in (u_lo, u_hi):
        for radial in (0.0, -hole_depth):
            for z in (0.0, hole_top):
                target = _pos_along_side(P1, P2, side_len, outward, u, radial, z)
                assert _find_vertex(verts, target) is not None, (
                    f"no hole vertex at {target}")


def test_bottom_plate_does_not_cover_cavity():
    # Cavities have no floor by design (mesh_builder.py — "no floor, no front"),
    # so the bottom plate must detour around each cavity footprint at z=0. A
    # naive fan from bcenter to a walk that dips through the cavity emits two
    # triangles per side whose interiors stray into the cavity, because the
    # polygon "sector minus cavity" is not star-shaped from bcenter. Sample a
    # grid of points strictly inside each cavity and assert no z=0 triangle
    # has any of them in its interior.
    diameter = 100.0
    verts, faces = build_hex_tile(diameter, 5.0, 10.0, (0,) * 6, None, 0)
    bottom_tris = [
        face for face in faces
        if len(face) == 3 and all(abs(verts[idx][2]) < 1e-9 for idx in face)
    ]
    assert len(bottom_tris) == 42  # 7 per side × 6 sides (5 fan + 2 ear)

    R = diameter / 2.0
    side_len = R
    u_hole_lo = TAB_OFFSET_FROM_CORNER_MM - TAB_HOLE_TOLERANCE_MM / 2.0
    u_hole_hi = TAB_OFFSET_FROM_CORNER_MM + TAB_WIDTH_MM + TAB_HOLE_TOLERANCE_MM / 2.0
    hole_depth = TAB_DEPTH_MM + TAB_HOLE_TOLERANCE_MM

    def strictly_inside(p, a, b, c, eps=1e-7):
        def s(p1, p2, p3):
            return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
        d1, d2, d3 = s(p, a, b), s(p, b, c), s(p, c, a)
        return (d1 > eps and d2 > eps and d3 > eps) or (d1 < -eps and d2 < -eps and d3 < -eps)

    for i in range(6):
        ip1 = (i + 1) % 6
        angle_i = math.pi / 3.0 - i * (math.pi / 3.0)
        angle_ip1 = math.pi / 3.0 - ip1 * (math.pi / 3.0)
        P_i = (R * math.cos(angle_i), R * math.sin(angle_i))
        P_ip1 = (R * math.cos(angle_ip1), R * math.sin(angle_ip1))
        mid = ((P_i[0] + P_ip1[0]) / 2.0, (P_i[1] + P_ip1[1]) / 2.0)
        mag = math.hypot(*mid)
        outward = (mid[0] / mag, mid[1] / mag)
        for u_frac in (0.1, 0.5, 0.9):
            for r_frac in (0.1, 0.5, 0.9):
                u = u_hole_lo + u_frac * (u_hole_hi - u_hole_lo)
                radial = -r_frac * hole_depth
                t = u / side_len
                px = P_i[0] + t * (P_ip1[0] - P_i[0]) + radial * outward[0]
                py = P_i[1] + t * (P_ip1[1] - P_i[1]) + radial * outward[1]
                for tri in bottom_tris:
                    a, b, c = verts[tri[0]], verts[tri[1]], verts[tri[2]]
                    assert not strictly_inside((px, py), a, b, c), (
                        f"bottom triangle {tri} covers cavity {i} sample ({px}, {py})")


def test_tab_mates_with_ne_neighbour_hole():
    # A's tab on side 0 must mate with B's hole on side 3, where B is A's NE
    # neighbour. The shared edge runs P2_A ↔ P4_B and P1_A ↔ P5_B (per
    # SHARED_CORNERS in map.py). A's tab is 10 mm from A.P2; B's hole is 10 mm
    # from B.P4 — those are the same point. The cavity is symmetric around
    # the mating tab on the side-direction axis (TOL/2 clearance each side)
    # and asymmetric on the radial axis (cavity goes 0.2 mm DEEPER than the
    # tab protrudes, with no clearance at the rim seam).
    diameter = 100.0
    verts_a, _ = build_hex_tile(diameter, 5.0, 10.0, (0,) * 6, None, 0)
    verts_b_local, _ = build_hex_tile(diameter, 5.0, 10.0, (0,) * 6, None, 0)
    qb, rb = neighbour_coord(0, 0, NE)
    bx, by = tile_world_xy(qb, rb, diameter)
    verts_b = [(v[0] + bx, v[1] + by, v[2]) for v in verts_b_local]

    R = diameter / 2.0
    side_len = R
    u_tab_lo = side_len - TAB_OFFSET_FROM_CORNER_MM - TAB_WIDTH_MM
    u_tab_hi = side_len - TAB_OFFSET_FROM_CORNER_MM
    u_hole_lo = TAB_OFFSET_FROM_CORNER_MM - TAB_HOLE_TOLERANCE_MM / 2.0
    u_hole_hi = TAB_OFFSET_FROM_CORNER_MM + TAB_WIDTH_MM + TAB_HOLE_TOLERANCE_MM / 2.0
    hole_depth = TAB_DEPTH_MM + TAB_HOLE_TOLERANCE_MM
    hole_top = TAB_HEIGHT_MM + TAB_HOLE_TOLERANCE_MM

    P1_a, P2_a, _, out_a0 = _side0_frame(diameter)

    # B's side 3 runs from P4 to P5 in B's local frame.
    def _local_corner(i, diameter):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        return (diameter / 2.0 * math.cos(angle),
                diameter / 2.0 * math.sin(angle))
    P4_b_local = _local_corner(3, diameter)
    P5_b_local = _local_corner(4, diameter)
    mid_b3 = ((P4_b_local[0] + P5_b_local[0]) / 2.0,
              (P4_b_local[1] + P5_b_local[1]) / 2.0)
    mag = math.hypot(*mid_b3)
    out_b3 = (mid_b3[0] / mag, mid_b3[1] / mag)

    # A's tab AABB centre vs B's hole AABB centre, in world coords.
    tab_centre = _pos_along_side(
        P1_a, P2_a, side_len, out_a0,
        (u_tab_lo + u_tab_hi) / 2.0,
        TAB_DEPTH_MM / 2.0,
        TAB_HEIGHT_MM / 2.0,
    )
    hole_centre_local = _pos_along_side(
        P4_b_local, P5_b_local, side_len, out_b3,
        (u_hole_lo + u_hole_hi) / 2.0,
        -hole_depth / 2.0,
        hole_top / 2.0,
    )
    hole_centre = (hole_centre_local[0] + bx,
                   hole_centre_local[1] + by,
                   hole_centre_local[2])
    # Centres differ at most by TOL on each axis (radial asymmetry contributes
    # TOL/2, z asymmetry contributes TOL/2, u axis is exactly symmetric).
    for axis in range(3):
        assert abs(tab_centre[axis] - hole_centre[axis]) <= TAB_HOLE_TOLERANCE_MM + 1e-9, (
            f"axis {axis}: tab={tab_centre}, hole={hole_centre}")

    # Each of A's 4 outer tab corners (the corners that go INTO B's cavity)
    # must sit inside B's hole AABB with ≥ 0 mm clearance on every axis-aligned
    # constraint that the cavity imposes.
    for u in (u_tab_lo, u_tab_hi):
        for z in (0.0, TAB_HEIGHT_MM):
            tab_corner = _pos_along_side(
                P1_a, P2_a, side_len, out_a0, u, TAB_DEPTH_MM, z)
            # Project into B's side-3 frame: (u_b, radial_b, z_b).
            # u_b along B-side: distance from P4_b_local in world.
            dx = tab_corner[0] - (P4_b_local[0] + bx)
            dy = tab_corner[1] - (P4_b_local[1] + by)
            side_dir_b = ((P5_b_local[0] - P4_b_local[0]) / side_len,
                          (P5_b_local[1] - P4_b_local[1]) / side_len)
            u_b = dx * side_dir_b[0] + dy * side_dir_b[1]
            radial_b = dx * out_b3[0] + dy * out_b3[1]
            z_b = tab_corner[2]
            # Cavity in B-frame: u_b in [u_hole_lo, u_hole_hi],
            # radial_b in [-hole_depth, 0], z_b in [0, hole_top].
            assert u_hole_lo - 1e-9 <= u_b <= u_hole_hi + 1e-9, (
                f"tab corner u_b={u_b} outside cavity [{u_hole_lo}, {u_hole_hi}]")
            assert -hole_depth - 1e-9 <= radial_b <= 0 + 1e-9, (
                f"tab corner radial_b={radial_b} outside cavity")
            assert 0 - 1e-9 <= z_b <= hole_top + 1e-9, (
                f"tab corner z_b={z_b} outside cavity")
