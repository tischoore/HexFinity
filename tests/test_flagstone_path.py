"""Tests for the Flagstone Path Feature's corridor polygon
(`procedural_surfaces.path_corridor_polygons`) and its use with the shared
`scatter_flagstones` tessellator."""

import math

import procedural_surfaces as ps

D = 100.0  # tile diameter, mm


def _area(poly):
    return ps._signed_area(poly)


def _inside(poly, x, y, eps=1e-6):
    # CCW convex-or-concave: winding test via ray casting.
    n, c = len(poly), False
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if xi > x - eps:
                c = not c
    return c


def test_straight_line_is_rectangle_of_width():
    polys = ps.path_corridor_polygons([(-20, 0), (20, 0)], 6.0, D)
    assert len(polys) == 1
    p = polys[0]
    assert _area(p) > 0
    assert math.isclose(_area(p), 40 * 6.0, rel_tol=1e-6)
    ys = [v[1] for v in p]
    assert math.isclose(max(ys), 3.0) and math.isclose(min(ys), -3.0)


def test_corridor_clipped_to_hex():
    polys = ps.path_corridor_polygons([(-80, 0), (80, 0)], 10.0, D)
    hexp = ps.hex_polygon(D)
    hexp_ccw = hexp if ps._signed_area(hexp) > 0 else hexp[::-1]
    for p in polys:
        for (x, y) in p:
            # every vertex on or inside the hexagon
            assert math.hypot(x, y) <= D / 2 + 1e-6
            for i in range(6):
                (x0, y0), (x1, y1) = hexp_ccw[i], hexp_ccw[(i + 1) % 6]
                cross = (x1 - x0) * (y - y0) - (y1 - y0) * (x - x0)
                assert cross >= -1e-6


def test_l_bend_single_simple_polygon():
    polys = ps.path_corridor_polygons([(-20, 0), (0, 0), (0, 20)], 6.0, D)
    assert len(polys) == 1
    assert ps._polygon_is_simple(polys[0])
    assert _area(polys[0]) > 0


def test_hairpin_falls_back_to_segment_quads():
    polys = ps.path_corridor_polygons([(-20, 0), (20, 0), (-20, 1)], 6.0, D)
    assert len(polys) == 2


def test_degenerate_inputs_return_empty():
    assert ps.path_corridor_polygons([(0, 0)], 6.0, D) == []
    assert ps.path_corridor_polygons([(0, 0), (0, 0)], 6.0, D) == []
    assert ps.path_corridor_polygons([(0, 0), (10, 0)], 0.0, D) == []


def test_flagstones_stay_inside_corridor():
    poly = ps.path_corridor_polygons([(-30, -10), (0, 5), (30, -5)], 12.0, D)[0]
    placements = ps.scatter_flagstones(poly, min_size_mm=3.0, max_size_mm=5.0,
                                       gap_mm=0.3, seed=7)
    assert placements
    for (x, y, outline, _pid) in placements:
        assert _inside(poly, x, y) or any(
            math.hypot(x - vx, y - vy) < 1e-3 for vx, vy in poly)
        for (lx, ly) in outline:
            # outline corners within the corridor (allow tiny float slack)
            px, py = x + lx, y + ly
            assert _inside(poly, px, py, eps=1e-3) or min(
                math.hypot(px - vx, py - vy) for vx, vy in poly) < 1e-2 \
                or _near_edge(poly, px, py)


def _near_edge(poly, x, y, tol=1e-3):
    n = len(poly)
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / L2))
        if math.hypot(x - (x0 + t * dx), y - (y0 + t * dy)) < tol:
            return True
    return False
