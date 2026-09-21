import pytest

import segment_geometry as sg


# ---------------------------------------------------------------------------
# convex_hull

def test_convex_hull_square_keeps_only_corners():
    points = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    hull = sg.convex_hull(points)
    assert set(hull) == set(points)
    assert len(hull) == 4


def test_convex_hull_triangle():
    points = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
    hull = sg.convex_hull(points)
    assert set(hull) == set(points)


def test_convex_hull_rejects_interior_points():
    points = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0), (0.0, 0.0)]
    hull = sg.convex_hull(points)
    assert (0.0, 0.0) not in hull
    assert len(hull) == 4


def test_convex_hull_drops_collinear_points_on_an_edge():
    points = [(-10.0, -10.0), (0.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    hull = sg.convex_hull(points)
    assert (0.0, -10.0) not in hull
    assert len(hull) == 4


def test_convex_hull_deduplicates_points():
    points = [(0.0, 0.0), (0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
    hull = sg.convex_hull(points)
    assert len(hull) == 3


def test_convex_hull_degenerate_input_returns_distinct_points():
    assert sg.convex_hull([(1.0, 1.0)]) == [(1.0, 1.0)]
    assert set(sg.convex_hull([(1.0, 1.0), (2.0, 2.0)])) == {(1.0, 1.0), (2.0, 2.0)}


def test_convex_hull_is_ordered_ccw():
    points = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    hull = sg.convex_hull(points)
    area2 = 0.0
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        area2 += x1 * y2 - x2 * y1
    assert area2 > 0.0  # positive signed area == counter-clockwise winding


# ---------------------------------------------------------------------------
# hull_edge_snap_targets

_SQUARE = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]


@pytest.mark.parametrize("edge_snap", [2, 3, 5])
def test_hull_edge_snap_targets_count(edge_snap):
    targets = sg.hull_edge_snap_targets(_SQUARE, edge_snap)
    assert len(targets) == len(_SQUARE) * (edge_snap - 1)


def test_hull_edge_snap_targets_edge_indices_in_range():
    targets = sg.hull_edge_snap_targets(_SQUARE, 3)
    for (x, y, edge_idx) in targets:
        assert 0 <= edge_idx < len(_SQUARE)


def test_hull_edge_snap_targets_vertices_tagged_with_owning_edge():
    # edge_snap=2 yields exactly one point per edge: the edge's start vertex.
    targets = sg.hull_edge_snap_targets(_SQUARE, 2)
    assert len(targets) == len(_SQUARE)
    for i, (x, y, edge_idx) in enumerate(targets):
        assert (x, y) == pytest.approx(_SQUARE[i])
        assert edge_idx == i


def test_hull_edge_snap_targets_includes_midpoints():
    targets = sg.hull_edge_snap_targets(_SQUARE, 3)
    xy_only = {(round(x, 6), round(y, 6)) for (x, y, _) in targets}
    for i in range(len(_SQUARE)):
        a = _SQUARE[i]
        b = _SQUARE[(i + 1) % len(_SQUARE)]
        mid = (round((a[0] + b[0]) / 2.0, 6), round((a[1] + b[1]) / 2.0, 6))
        assert mid in xy_only


# ---------------------------------------------------------------------------
# nearest_point_on_hull_edge
# _SQUARE edges: 0 = bottom (y=-10), 1 = right (x=10), 2 = top (y=10),
# 3 = left (x=-10).

def test_nearest_point_on_hull_edge_unconstrained_picks_closest_edge():
    x, y, edge_idx = sg.nearest_point_on_hull_edge(2.0, -8.0, _SQUARE)
    assert (x, y) == pytest.approx((2.0, -10.0))
    assert edge_idx == 0


def test_nearest_point_on_hull_edge_lock_x_solves_y_on_nearest_crossing():
    # x=5 crosses both the bottom (edge 0) and top (edge 2) edges; the point
    # sits closer to the bottom, so the solved y should land there.
    x, y, edge_idx = sg.nearest_point_on_hull_edge(
        5.0, -9.0, _SQUARE, lock_x=True)
    assert x == pytest.approx(5.0)
    assert y == pytest.approx(-10.0)
    assert edge_idx == 0


def test_nearest_point_on_hull_edge_lock_y_solves_x_on_nearest_crossing():
    x, y, edge_idx = sg.nearest_point_on_hull_edge(
        -9.0, 5.0, _SQUARE, lock_y=True)
    assert x == pytest.approx(-10.0)
    assert y == pytest.approx(5.0)
    assert edge_idx == 3


def test_nearest_point_on_hull_edge_lock_x_outside_range_returns_none():
    assert sg.nearest_point_on_hull_edge(20.0, 0.0, _SQUARE, lock_x=True) is None


def test_nearest_point_on_hull_edge_both_locked_returns_none():
    assert sg.nearest_point_on_hull_edge(
        2.0, -8.0, _SQUARE, lock_x=True, lock_y=True) is None


def test_nearest_point_on_hull_edge_degenerate_hull_returns_none():
    assert sg.nearest_point_on_hull_edge(0.0, 0.0, [(0.0, 0.0)]) is None
    assert sg.nearest_point_on_hull_edge(0.0, 0.0, []) is None


# ---------------------------------------------------------------------------
# sharp_hull_corner_points

def test_sharp_hull_corner_points_square_all_corners_sharp_at_default():
    sharp = sg.sharp_hull_corner_points(_SQUARE, 50.0)
    assert {i for (_, _, i) in sharp} == {0, 1, 2, 3}
    assert {(x, y) for (x, y, _) in sharp} == set(_SQUARE)


def test_sharp_hull_corner_points_square_not_sharp_above_90():
    # A square's corners turn exactly 90 degrees, so a 100-degree threshold
    # should flag none of them.
    assert sg.sharp_hull_corner_points(_SQUARE, 100.0) == []


def test_sharp_hull_corner_points_degenerate_returns_empty():
    assert sg.sharp_hull_corner_points([], 50.0) == []
    assert sg.sharp_hull_corner_points([(0.0, 0.0), (1.0, 0.0)], 50.0) == []


def test_sharp_hull_corner_points_collinear_vertex_never_sharp():
    # A point sitting exactly on a straight run between its neighbours has
    # a turn of 0 degrees, so it must never be flagged, even at a very low
    # threshold.
    poly = [(-10.0, -10.0), (0.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    sharp = sg.sharp_hull_corner_points(poly, 1.0)
    assert 1 not in {i for (_, _, i) in sharp}  # (0.0, -10.0) is collinear


def test_sharp_hull_corner_points_skips_zero_length_edge():
    poly = [(-10.0, -10.0), (-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    # Should not raise despite the duplicated vertex creating a zero-length edge.
    sg.sharp_hull_corner_points(poly, 50.0)


# ---------------------------------------------------------------------------
# polygon_edge_midpoints

def test_polygon_edge_midpoints_square_includes_wraparound_edge():
    mids = sg.polygon_edge_midpoints(_SQUARE)
    assert len(mids) == len(_SQUARE)
    for i in range(len(_SQUARE)):
        a = _SQUARE[i]
        b = _SQUARE[(i + 1) % len(_SQUARE)]
        expected = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        x, y, edge_idx = mids[i]
        assert (x, y) == pytest.approx(expected)
        assert edge_idx == i


def test_polygon_edge_midpoints_degenerate_returns_empty():
    assert sg.polygon_edge_midpoints([]) == []
    assert sg.polygon_edge_midpoints([(0.0, 0.0)]) == []


# ---------------------------------------------------------------------------
# point_in_polygon_concave

def test_point_in_polygon_concave_matches_convex_case():
    from map import point_in_polygon
    assert sg.point_in_polygon_concave(0.0, 0.0, _SQUARE) == point_in_polygon(
        0.0, 0.0, _SQUARE)
    assert sg.point_in_polygon_concave(50.0, 0.0, _SQUARE) == point_in_polygon(
        50.0, 0.0, _SQUARE)


def test_point_in_polygon_concave_handles_l_shape():
    # An L-shaped polygon: a 20x20 square with the top-right 10x10 quadrant
    # notched out.
    l_shape = [
        (0.0, 0.0), (20.0, 0.0), (20.0, 10.0),
        (10.0, 10.0), (10.0, 20.0), (0.0, 20.0),
    ]
    assert sg.point_in_polygon_concave(5.0, 5.0, l_shape) is True  # solid arm
    assert sg.point_in_polygon_concave(15.0, 15.0, l_shape) is False  # in the notch


def test_point_in_polygon_concave_degenerate_returns_false():
    assert sg.point_in_polygon_concave(0.0, 0.0, [(0.0, 0.0), (1.0, 0.0)]) is False


# ---------------------------------------------------------------------------
# fit_tilt_plane

def test_fit_tilt_plane_exact_through_three_points():
    # z = 2 + 0.5*x - 0.25*y, sampled at 3 non-collinear (x, y).
    def plane(x, y):
        return 2.0 + 0.5 * x - 0.25 * y

    pts = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
    samples = [(x, y, plane(x, y)) for (x, y) in pts]
    result = sg.fit_tilt_plane(samples)
    assert result is not None
    px, py, pz, slope_x, slope_y = result
    assert slope_x == pytest.approx(0.5)
    assert slope_y == pytest.approx(-0.25)
    # The unconstrained fit's pivot is the samples' centroid/mean.
    assert (px, py) == pytest.approx((10.0 / 3.0, 10.0 / 3.0))
    assert pz == pytest.approx(sum(plane(x, y) for (x, y) in pts) / 3.0)


def test_fit_tilt_plane_least_squares_four_points():
    # A perfect plane plus one point bumped off it -- the fit should land
    # near, but not exactly on, the underlying plane's slope.
    def plane(x, y):
        return 1.0 + 0.2 * x + 0.1 * y

    pts = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    samples = [(x, y, plane(x, y)) for (x, y) in pts]
    samples[0] = (samples[0][0], samples[0][1], samples[0][2] + 4.0)
    result = sg.fit_tilt_plane(samples)
    assert result is not None
    _, _, _, slope_x, slope_y = result
    assert slope_x == pytest.approx(0.2, abs=0.15)
    assert slope_y == pytest.approx(0.1, abs=0.15)


def test_fit_tilt_plane_pinned_pivot_passes_through_exactly():
    pts = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
    samples = [(x, y, x - y) for (x, y) in pts]  # not perfectly planar-free of noise here, but linear anyway
    pivot_xy = (0.0, 0.0)
    pivot_z = 5.0  # deliberately NOT samples[0]'s actual sampled z (0.0)
    result = sg.fit_tilt_plane(samples, pivot_xy, pivot_z)
    assert result is not None
    px, py, pz, slope_x, slope_y = result
    assert (px, py, pz) == pytest.approx((0.0, 0.0, 5.0))


def test_fit_tilt_plane_fewer_than_three_samples_returns_none():
    assert sg.fit_tilt_plane([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]) is None
    assert sg.fit_tilt_plane([]) is None


def test_fit_tilt_plane_collinear_samples_returns_none():
    samples = [(0.0, 0.0, 0.0), (5.0, 0.0, 1.0), (10.0, 0.0, 2.0)]
    assert sg.fit_tilt_plane(samples) is None


# ---------------------------------------------------------------------------
# fit_resting_plane

def _plane_height(fit, x, y):
    px, py, pz, slope_x, slope_y = fit
    return pz + slope_x * (x - px) + slope_y * (y - py)


def test_fit_resting_plane_exact_for_three_points():
    samples = [(0.0, 0.0, 1.0), (10.0, 0.0, 3.0), (0.0, 10.0, -2.0)]
    fit = sg.fit_resting_plane(samples)
    assert fit is not None
    for (x, y, z) in samples:
        assert _plane_height(fit, x, y) == pytest.approx(z, abs=1e-9)


def test_fit_resting_plane_never_goes_below_any_sample():
    samples = [(-10.0, -10.0, 1.0), (10.0, -10.0, 4.0), (10.0, 10.0, 0.0),
               (-10.0, 10.0, 2.0), (0.0, 0.0, 3.0)]
    fit = sg.fit_resting_plane(samples)
    assert fit is not None
    for (x, y, z) in samples:
        assert _plane_height(fit, x, y) >= z - 1e-6


def test_fit_resting_plane_touches_at_least_three_samples():
    samples = [(-10.0, -10.0, 2.0), (10.0, -10.0, -2.0), (10.0, 10.0, 2.0),
               (-10.0, 10.0, -2.0)]
    fit = sg.fit_resting_plane(samples)
    assert fit is not None
    touching = [1 for (x, y, z) in samples
               if abs(_plane_height(fit, x, y) - z) < 1e-6]
    assert sum(touching) >= 3, touching


def test_fit_resting_plane_coplanar_samples_exact_everywhere():
    def plane(x, y):
        return 2.0 + 0.5 * x - 0.3 * y

    samples = [(x, y, plane(x, y)) for (x, y) in
              [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]]
    fit = sg.fit_resting_plane(samples)
    assert fit is not None
    for (x, y, z) in samples:
        assert _plane_height(fit, x, y) == pytest.approx(z, abs=1e-6)


def test_fit_resting_plane_pinned_anchor_exact():
    samples = [(-10.0, -10.0, 1.0), (10.0, -10.0, 4.0), (10.0, 10.0, 0.0),
               (-10.0, 10.0, 2.0)]
    anchor_xy, anchor_z = (0.0, 0.0), 5.0  # deliberately not on any sample's plane
    fit = sg.fit_resting_plane(samples, anchor_xy, anchor_z)
    assert fit is not None
    assert _plane_height(fit, anchor_xy[0], anchor_xy[1]) == pytest.approx(anchor_z, abs=1e-6)
    for (x, y, z) in samples:
        assert _plane_height(fit, x, y) >= z - 1e-6


def test_fit_resting_plane_table_wobble_known_case():
    # A rigid square with 3 corners flat (z=0) and one corner (D) raised
    # to z=5. A flat plane through A/B/C would have D poking through it
    # (invalid -- the object would need to clip through the raised
    # corner), so the resting plane must tilt, touching 3 of the 4
    # corners with the 4th floating a gap above -- hand-verified: the
    # valid resting planes both have a max gap of exactly 5mm.
    a, b, c, d = (0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 5.0)
    samples = [a, b, c, d]
    fit = sg.fit_resting_plane(samples)
    assert fit is not None
    gaps = {(x, y): _plane_height(fit, x, y) - z for (x, y, z) in samples}
    assert all(g >= -1e-6 for g in gaps.values()), gaps
    assert max(gaps.values()) == pytest.approx(5.0, abs=1e-6)
    touching = [pt for pt, g in gaps.items() if abs(g) < 1e-6]
    assert len(touching) == 3, touching


def test_fit_resting_plane_degenerate_collinear_returns_none():
    samples = [(0.0, 0.0, 0.0), (5.0, 0.0, 1.0), (10.0, 0.0, 2.0)]
    assert sg.fit_resting_plane(samples) is None


def test_fit_resting_plane_fewer_than_three_samples_returns_none():
    assert sg.fit_resting_plane([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]) is None
    assert sg.fit_resting_plane([]) is None


# ---------------------------------------------------------------------------
# clamp_tilt_slopes

def test_clamp_tilt_slopes_within_range_unchanged():
    assert sg.clamp_tilt_slopes(0.1, -0.05, 30.0) == (0.1, -0.05)


def test_clamp_tilt_slopes_scales_down_preserving_direction():
    import math
    slope_x, slope_y = 5.0, 0.0  # atan(5) ~= 78.7 degrees, well over 30
    cx, cy = sg.clamp_tilt_slopes(slope_x, slope_y, 30.0)
    assert cx == pytest.approx(math.tan(math.radians(30.0)))
    assert cy == pytest.approx(0.0)
    angle = math.degrees(math.atan(math.hypot(cx, cy)))
    assert angle == pytest.approx(30.0)


def test_clamp_tilt_slopes_zero_slope_unchanged():
    assert sg.clamp_tilt_slopes(0.0, 0.0, 30.0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# hull_prism_triangles

def test_hull_prism_triangles_count_and_z_range():
    tris = sg.hull_prism_triangles(_SQUARE, 0.0, 10.0)
    # 4-vertex hull: (4-2)*2 = 4 cap triangles + 4*2 = 8 side triangles.
    assert len(tris) == 12
    zs = {v[2] for tri in tris for v in tri}
    assert zs == {0.0, 10.0}


def test_hull_prism_triangles_degenerate_returns_empty():
    assert sg.hull_prism_triangles([], 0.0, 10.0) == []
    assert sg.hull_prism_triangles([(0.0, 0.0), (1.0, 0.0)], 0.0, 10.0) == []


def test_hull_prism_triangles_caps_wound_outward():
    # Top cap triangles use hull_vertices' own (CCW) order -> a positive
    # signed 2D area, mirroring how the source hull itself winds CCW.
    tris = sg.hull_prism_triangles(_SQUARE, 0.0, 10.0)
    top_tris = [tri for tri in tris if all(v[2] == 10.0 for v in tri)]
    for (a, b, c) in top_tris:
        area2 = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
        assert area2 > 0.0
    bottom_tris = [tri for tri in tris if all(v[2] == 0.0 for v in tri)]
    for (a, b, c) in bottom_tris:
        area2 = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
        assert area2 < 0.0
