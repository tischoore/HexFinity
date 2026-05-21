import math
import pytest

from mesh_builder import build_hex_tile
from manifold_check import assert_two_manifold


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
        (0, 14),   # N=1: top 7 + bottom 7
        (1, 32),   # N=2: top 19 + bottom 13
        (2, 56),   # N=3: top 37 + bottom 19
        (4, 122),  # N=5: top 91 + bottom 31
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
