import pytest

from manifold_check import assert_two_manifold, ManifoldError


CUBE_VERTS = [
    (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
    (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1),
]
CUBE_FACES = [
    (0, 3, 2, 1),  # bottom
    (4, 5, 6, 7),  # top
    (0, 1, 5, 4),  # front
    (1, 2, 6, 5),  # right
    (2, 3, 7, 6),  # back
    (3, 0, 4, 7),  # left
]


def test_passes_for_closed_cube():
    assert_two_manifold(CUBE_VERTS, CUBE_FACES)


def test_fails_when_a_face_is_missing():
    faces = list(CUBE_FACES)
    faces.pop()  # remove left face -> 4 edges shared by only 1 face
    with pytest.raises(ManifoldError):
        assert_two_manifold(CUBE_VERTS, faces)


def test_fails_for_orphan_vertex():
    verts = list(CUBE_VERTS) + [(99.0, 99.0, 99.0)]
    with pytest.raises(ManifoldError):
        assert_two_manifold(verts, CUBE_FACES)


def test_fails_when_edge_shared_by_three_faces():
    # Add a duplicate face sharing all edges with the bottom -> those 4 edges
    # are now shared by 3 faces.
    faces = list(CUBE_FACES) + [(0, 3, 2, 1)]
    with pytest.raises(ManifoldError):
        assert_two_manifold(CUBE_VERTS, faces)


def test_fails_for_degenerate_face_edge():
    faces = list(CUBE_FACES) + [(0, 0, 1)]
    with pytest.raises(ManifoldError):
        assert_two_manifold(CUBE_VERTS, faces)


def test_fails_for_face_with_fewer_than_three_vertices():
    faces = list(CUBE_FACES) + [(0, 1)]
    with pytest.raises(ManifoldError):
        assert_two_manifold(CUBE_VERTS, faces)
