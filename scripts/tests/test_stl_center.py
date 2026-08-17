"""Tests for the bpy-free logic in ``scripts/stl_center.py``.

Exercise the pure helpers only (find_stl_files, compute_offset) — no bpy
involved, so these run under any CPython (including Blender's bundled
interpreter):

    python -m pytest scripts/tests -v
"""

import importlib.util
from pathlib import Path

import pytest

# Import the standalone script by path (it lives in scripts/, not on sys.path).
_SCRIPT = Path(__file__).resolve().parents[1] / "stl_center.py"
_spec = importlib.util.spec_from_file_location("stl_center", _SCRIPT)
stl_center = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stl_center)


# --------------------------------------------------------------------------- #
# find_stl_files
# --------------------------------------------------------------------------- #

def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def test_find_stl_files_top_level_only(tmp_path):
    _touch(tmp_path / "a.stl")
    _touch(tmp_path / "b.STL")
    _touch(tmp_path / "c.txt")
    _touch(tmp_path / "sub" / "d.stl")

    found = stl_center.find_stl_files(tmp_path, recursive=False)
    assert found == [tmp_path / "a.stl", tmp_path / "b.STL"]


def test_find_stl_files_recursive(tmp_path):
    _touch(tmp_path / "a.stl")
    _touch(tmp_path / "sub" / "d.stl")
    _touch(tmp_path / "sub" / "nested" / "e.stl")

    found = stl_center.find_stl_files(tmp_path, recursive=True)
    assert found == sorted([
        tmp_path / "a.stl",
        tmp_path / "sub" / "d.stl",
        tmp_path / "sub" / "nested" / "e.stl",
    ])


def test_find_stl_files_empty_folder(tmp_path):
    assert stl_center.find_stl_files(tmp_path, recursive=False) == []
    assert stl_center.find_stl_files(tmp_path, recursive=True) == []


def test_find_stl_files_ignores_non_stl(tmp_path):
    _touch(tmp_path / "readme.txt")
    _touch(tmp_path / "model.obj")
    assert stl_center.find_stl_files(tmp_path, recursive=True) == []


# --------------------------------------------------------------------------- #
# compute_offset
# --------------------------------------------------------------------------- #

def test_compute_offset_center():
    offset = stl_center.compute_offset((0.0, -10.0, 2.0), (20.0, 10.0, 8.0),
                                       bottom=False)
    assert offset == (10.0, 0.0, 5.0)


def test_compute_offset_bottom():
    offset = stl_center.compute_offset((0.0, -10.0, 2.0), (20.0, 10.0, 8.0),
                                       bottom=True)
    assert offset == (10.0, 0.0, 2.0)


def test_compute_offset_zero_size_bbox():
    offset = stl_center.compute_offset((5.0, 5.0, 5.0), (5.0, 5.0, 5.0),
                                       bottom=False)
    assert offset == (5.0, 5.0, 5.0)
    offset_bottom = stl_center.compute_offset((5.0, 5.0, 5.0), (5.0, 5.0, 5.0),
                                              bottom=True)
    assert offset_bottom == (5.0, 5.0, 5.0)
