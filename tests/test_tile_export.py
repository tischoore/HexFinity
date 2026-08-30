import pytest

import tile_export as te


# A trivial two-triangle quad used as a stand-in mesh.
QUAD_VERTS = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
QUAD_FACES = [(0, 1, 2), (0, 2, 3)]


# ---------------------------------------------------------------------------
# tile_geometry_hash — determinism & sensitivity

def test_hash_is_deterministic():
    h1 = te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES)
    h2 = te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES)
    assert h1 == h2


def test_hash_changes_with_geometry():
    moved = list(QUAD_VERTS)
    moved[2] = (1.0, 2.0, 0.0)  # well above the quantization threshold
    assert te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES) \
        != te.tile_geometry_hash(moved, QUAD_FACES)


def test_hash_changes_with_topology():
    other_faces = [(0, 1, 2), (1, 2, 3)]
    assert te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES) \
        != te.tile_geometry_hash(QUAD_VERTS, other_faces)


def test_hash_absorbs_subthreshold_jitter():
    # Float noise below half the quantization step must not change the bucket.
    jittered = [(x + 2.0e-5, y - 2.0e-5, z) for (x, y, z) in QUAD_VERTS]
    assert te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES) \
        == te.tile_geometry_hash(jittered, QUAD_FACES)


# ---------------------------------------------------------------------------
# tile_geometry_hash — children

def test_children_change_hash():
    child = [(QUAD_VERTS, QUAD_FACES)]
    assert te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES) \
        != te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES, child)


def test_child_order_is_irrelevant():
    a_verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    b_verts = [(5.0, 5.0, 5.0), (6.0, 5.0, 5.0), (5.0, 6.0, 5.0)]
    tri = [(0, 1, 2)]
    forward = [(a_verts, tri), (b_verts, tri)]
    reversed_ = [(b_verts, tri), (a_verts, tri)]
    assert te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES, forward) \
        == te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES, reversed_)


# ---------------------------------------------------------------------------
# short_hash

def test_short_hash_length():
    digest = te.tile_geometry_hash(QUAD_VERTS, QUAD_FACES)
    assert len(te.short_hash(digest)) == 8
    assert len(te.short_hash(digest, 12)) == 12
    assert digest.startswith(te.short_hash(digest))


# ---------------------------------------------------------------------------
# is_custom_tile

@pytest.mark.parametrize("children,brush,snap,regions,expected", [
    (False, False, False, 0, False),
    (True, False, False, 0, True),
    (False, True, False, 0, True),
    (False, False, True, 0, True),
    (False, False, False, 1, True),
    (True, True, True, 3, True),
])
def test_is_custom_tile(children, brush, snap, regions, expected):
    assert te.is_custom_tile(children, brush, snap, regions) is expected


# ---------------------------------------------------------------------------
# tile_filename

def test_filename_plain():
    assert te.tile_filename(0, 0, False, "deadbeef") == "hex_q00_r00.stl"


def test_filename_custom_appends_hash():
    assert te.tile_filename(3, 5, True, "deadbeef") == "hex_q03_r05_deadbeef.stl"


def test_filename_zero_pads_coordinates():
    assert te.tile_filename(12, 7, False, "x") == "hex_q12_r07.stl"


# ---------------------------------------------------------------------------
# manifest_rows

def test_manifest_rows_normalize_and_sort():
    records = [
        {"q": 1, "r": 0, "file": "b.stl", "custom": True},
        {"q": 0, "r": 0, "file": "a.stl", "custom": False},
        {"q": 0, "r": 2, "file": "c.stl", "custom": False},
    ]
    rows = te.manifest_rows(records)
    assert [(r["q"], r["r"]) for r in rows] == [(0, 0), (0, 2), (1, 0)]
    assert rows[0] == {"q": 0, "r": 0, "file": "a.stl", "custom": False}
    assert all(isinstance(r["custom"], bool) for r in rows)


# ---------------------------------------------------------------------------
# flora_tree_filename / flora_pin_filename

def test_flora_tree_filename_format():
    assert te.flora_tree_filename(3, 5, 2) == "hex_q03_r05_tree02.stl"


def test_flora_pin_filename_format():
    assert te.flora_pin_filename(3, 5, 2) == "hex_q03_r05_tree02_pin.stl"


def test_flora_filename_zero_pads():
    assert te.flora_tree_filename(12, 7, 0) == "hex_q12_r07_tree00.stl"
    assert te.flora_pin_filename(12, 7, 0) == "hex_q12_r07_tree00_pin.stl"


# ---------------------------------------------------------------------------
# flora_manifest_rows

def test_flora_manifest_rows_normalize_and_sort():
    records = [
        {"q": 1, "r": 0, "index": 0, "tree_file": "b.stl", "pin_file": "b_pin.stl"},
        {"q": 0, "r": 0, "index": 1, "tree_file": "a1.stl", "pin_file": "a1_pin.stl"},
        {"q": 0, "r": 0, "index": 0, "tree_file": "a0.stl", "pin_file": "a0_pin.stl"},
    ]
    rows = te.flora_manifest_rows(records)
    assert [(r["q"], r["r"], r["index"]) for r in rows] == [
        (0, 0, 0), (0, 0, 1), (1, 0, 0)]
    assert rows[0] == {"q": 0, "r": 0, "index": 0,
                       "tree_file": "a0.stl", "pin_file": "a0_pin.stl"}
