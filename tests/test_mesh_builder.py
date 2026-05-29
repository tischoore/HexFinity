import math
import pytest

from mesh_builder import (
    build_hex_tile,
    clamp_center_to_hexagon,
    TAB_WIDTH_MM,
    TAB_HEIGHT_MM,
    TAB_DEPTH_MM,
    TAB_OFFSET_FROM_CORNER_MM,
    TAB_HOLE_TOLERANCE_MM,
)
from manifold_check import assert_two_manifold
from map import neighbour_coord, tile_world_xy, NE


def _build(
    smoothness_passes=2,
    resample_density=0,
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
        smoothness_passes=smoothness_passes,
        resample_density=resample_density,
    )


@pytest.mark.parametrize(
    "smoothness_passes,expected_v",
    [
        # Top: Loop subdivision of the 13/18/30 control mesh. V grows by
        # +E each pass; E grows by 2·E + 3·F. Bottom: 103 verts (6 bcorners
        # + 24 bbreaks + 1 bcenter + 36 tab + 36 hole, with shared keys).
        (0, 13 + 103),    # control mesh (13)
        (1, 43 + 103),
        (2, 157 + 103),
        (3, 601 + 103),
    ],
)
def test_vertex_count(smoothness_passes, expected_v):
    verts, _ = _build(smoothness_passes=smoothness_passes)
    assert len(verts) == expected_v


@pytest.mark.parametrize(
    "smoothness_passes,expected_f",
    [
        # Top: 18 · 4^L. Bottom region: 6 side-wall n-gons + 30 tab faces
        # (5/side) + 24 cavity faces (4/side) + 42 bottom-plate (7/side) = 102.
        (0, 18 + 102),
        (1, 72 + 102),
        (2, 288 + 102),
        (3, 1152 + 102),
    ],
)
def test_face_count(smoothness_passes, expected_f):
    _, faces = _build(smoothness_passes=smoothness_passes)
    assert len(faces) == expected_f


@pytest.mark.parametrize("smoothness_passes", [0, 1, 2, 3])
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
def test_manifold(smoothness_passes, corner_levels):
    verts, faces = _build(smoothness_passes=smoothness_passes,
                          corner_levels=corner_levels)
    assert_two_manifold(verts, faces)


def test_manifold_with_center_override():
    verts, faces = _build(corner_levels=(0, 2, 4, 2, 0, 0), center_level=6)
    assert_two_manifold(verts, faces)


def test_euler_characteristic_is_sphere():
    # Closed 2-manifold homeomorphic to a sphere: V - E + F = 2.
    verts, faces = _build(smoothness_passes=2, corner_levels=(0, 1, 2, 1, 0, 0))
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
        smoothness_passes=0,
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
        smoothness_passes=0,
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
        smoothness_passes=0,
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
    # Negative smoothness_passes / resample_density.
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 10.0, (0,) * 6, None, -1)
    with pytest.raises(ValueError):
        build_hex_tile(100.0, 5.0, 10.0, (0,) * 6, None, 0, resample_density=-1)
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
    # At smoothness=0 the control mesh is emitted verbatim, so C sits at
    # center_xy with its raw level-derived z. With corners at level 0 and
    # C overridden to level 4, C is the unique top vertex above base
    # thickness — its XY must equal the requested center_xy in mm.
    verts, _ = build_hex_tile(
        diameter_mm=100.0,
        level_height_mm=5.0,
        base_thickness_mm=10.0,
        corner_levels=(0,) * 6,
        center_level=4,
        smoothness_passes=0,
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
        smoothness_passes=2,
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
