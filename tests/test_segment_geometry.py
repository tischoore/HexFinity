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
