import math
import pytest

import map as hm


# ---------------------------------------------------------------------------
# tile_world_xy

@pytest.mark.parametrize("q,r,expected", [
    (0, 0, (0.0, 0.0)),
    # Odd q → +half-row Y offset.
    (1, 0, (75.0, 25.0 * math.sqrt(3.0))),
    (0, 1, (0.0, 50.0 * math.sqrt(3.0))),
    (2, 1, (150.0, 50.0 * math.sqrt(3.0))),
    (1, 1, (75.0, 75.0 * math.sqrt(3.0))),
    (3, -2, (225.0, -75.0 * math.sqrt(3.0))),
])
def test_tile_world_xy(q, r, expected):
    got = hm.tile_world_xy(q, r, diameter_mm=100.0)
    assert got[0] == pytest.approx(expected[0], abs=1e-9)
    assert got[1] == pytest.approx(expected[1], abs=1e-9)


def test_tile_world_xy_scales_with_diameter():
    a = hm.tile_world_xy(3, 4, diameter_mm=100.0)
    b = hm.tile_world_xy(3, 4, diameter_mm=200.0)
    assert b[0] == pytest.approx(2.0 * a[0], abs=1e-9)
    assert b[1] == pytest.approx(2.0 * a[1], abs=1e-9)


# ---------------------------------------------------------------------------
# neighbour_coord (odd-q offset rules)

@pytest.mark.parametrize("direction,expected", [
    (hm.N,  (0, 1)),
    (hm.NE, (1, 0)),
    (hm.SE, (1, -1)),
    (hm.S,  (0, -1)),
    (hm.SW, (-1, -1)),
    (hm.NW, (-1, 0)),
])
def test_neighbour_coord_even_column(direction, expected):
    assert hm.neighbour_coord(0, 0, direction) == expected


@pytest.mark.parametrize("direction,expected", [
    (hm.N,  (1, 1)),
    (hm.NE, (2, 1)),
    (hm.SE, (2, 0)),
    (hm.S,  (1, -1)),
    (hm.SW, (0, 0)),
    (hm.NW, (0, 1)),
])
def test_neighbour_coord_odd_column(direction, expected):
    assert hm.neighbour_coord(1, 0, direction) == expected


@pytest.mark.parametrize("q,r", [(0, 0), (1, 0), (2, 3), (3, -2), (-1, 5), (-4, -7)])
@pytest.mark.parametrize("direction", list(hm.DIRECTIONS))
def test_neighbour_coord_round_trip(q, r, direction):
    # Stepping in `direction` then in OPPOSITE[direction] must return to (q, r)
    # for every parity. This catches off-by-one errors in the parity branches.
    nq, nr = hm.neighbour_coord(q, r, direction)
    back = hm.neighbour_coord(nq, nr, hm.OPPOSITE[direction])
    assert back == (q, r), (
        f"({q},{r}) {direction} → ({nq},{nr}) {hm.OPPOSITE[direction]} → {back}"
    )


def test_unknown_direction_raises():
    with pytest.raises(ValueError):
        hm.neighbour_coord(0, 0, "X")


# ---------------------------------------------------------------------------
# SHARED_CORNERS shape and symmetry

def test_shared_corners_table_shape():
    assert len(hm.SHARED_CORNERS) == 6
    for corner_idx, partners in enumerate(hm.SHARED_CORNERS):
        assert len(partners) == 2, (
            f"P{corner_idx+1}: expected 2 shared-corner entries, got {len(partners)}"
        )
        for direction, neighbour_corner_idx in partners:
            assert direction in hm.DIRECTIONS
            assert 0 <= neighbour_corner_idx < 6


def test_shared_corners_symmetric():
    # Every entry (T.Pi → dir.N.Pj) must be matched by an entry on the
    # neighbour: (N.Pj → OPPOSITE[dir].Pi). This is the property that makes
    # the propagation cascade correct — without it, a write to one tile's
    # corner would not converge to the others' corners.
    for corner_idx, partners in enumerate(hm.SHARED_CORNERS):
        for direction, neighbour_corner_idx in partners:
            inverse = (hm.OPPOSITE[direction], corner_idx)
            partners_back = hm.SHARED_CORNERS[neighbour_corner_idx]
            assert inverse in partners_back, (
                f"P{corner_idx+1} → {direction}.P{neighbour_corner_idx+1} has no "
                f"inverse {inverse} in SHARED_CORNERS[P{neighbour_corner_idx+1}] "
                f"= {partners_back}"
            )


def test_shared_corner_world_positions_coincide():
    # The geometric soundness check: for every tile T and every corner Pi,
    # the world XY of Pi must equal the world XY of the matching corner on
    # each of T's shared-corner neighbours. Mixes even and odd columns so
    # all parity branches in neighbour_coord get exercised.
    diameter = 100.0
    R = diameter / 2.0
    # Flat-top P-corner offsets relative to tile centre (must match
    # mesh_builder.build_hex_tile's angle convention).
    p_offset = []
    for i in range(6):
        angle = math.pi / 3.0 - i * (math.pi / 3.0)
        p_offset.append((R * math.cos(angle), R * math.sin(angle)))

    for (q, r) in [(0, 0), (1, 0), (1, 1), (2, 2), (-1, -1), (3, -2)]:
        tx, ty = hm.tile_world_xy(q, r, diameter)
        for corner_idx, partners in enumerate(hm.SHARED_CORNERS):
            ox, oy = p_offset[corner_idx]
            my_x, my_y = tx + ox, ty + oy
            for direction, n_corner_idx in partners:
                nq, nr = hm.neighbour_coord(q, r, direction)
                nx, ny = hm.tile_world_xy(nq, nr, diameter)
                nox, noy = p_offset[n_corner_idx]
                their_x, their_y = nx + nox, ny + noy
                assert their_x == pytest.approx(my_x, abs=1e-9), (
                    f"({q},{r}).P{corner_idx+1} vs "
                    f"({nq},{nr}).P{n_corner_idx+1} via {direction}: "
                    f"X {their_x} != {my_x}"
                )
                assert their_y == pytest.approx(my_y, abs=1e-9), (
                    f"({q},{r}).P{corner_idx+1} vs "
                    f"({nq},{nr}).P{n_corner_idx+1} via {direction}: "
                    f"Y {their_y} != {my_y}"
                )


# ---------------------------------------------------------------------------
# find_tile (uses tiny duck-typed stand-ins so we don't need bpy)

class _FakeProps:
    def __init__(self, q, r, is_generated=True):
        self.coord_q = q
        self.coord_r = r
        self.is_generated = is_generated


class _FakeObject:
    def __init__(self, q, r, is_generated=True):
        self.hexfinity_tile = _FakeProps(q, r, is_generated)


class _FakeCollection:
    def __init__(self, objects):
        self.objects = objects


class _FakeMapProps:
    def __init__(self, root_collection):
        self.root_collection = root_collection


class _FakeScene:
    def __init__(self, objects):
        self.hexfinity_map = _FakeMapProps(_FakeCollection(objects))


def test_find_tile_returns_match():
    scene = _FakeScene([_FakeObject(0, 0), _FakeObject(1, 2), _FakeObject(3, 4)])
    assert hm.find_tile(scene, 1, 2) is scene.hexfinity_map.root_collection.objects[1]


def test_find_tile_returns_none_for_missing():
    scene = _FakeScene([_FakeObject(0, 0), _FakeObject(1, 1)])
    assert hm.find_tile(scene, 5, 5) is None


def test_find_tile_skips_ungenerated():
    obj_a = _FakeObject(2, 3, is_generated=False)
    obj_b = _FakeObject(2, 3, is_generated=True)
    scene = _FakeScene([obj_a, obj_b])
    assert hm.find_tile(scene, 2, 3) is obj_b


def test_find_tile_handles_missing_root_collection():
    scene = _FakeScene([])
    scene.hexfinity_map.root_collection = None
    assert hm.find_tile(scene, 0, 0) is None
