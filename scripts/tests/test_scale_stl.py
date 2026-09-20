"""Tests for `scripts/scale_stl.py`.

Fully bpy-free — no bpy involved anywhere in this script, so these run under
any CPython (including Blender's bundled interpreter):

    python -m pytest scripts/tests -v
"""

import importlib.util
import struct
from pathlib import Path

import pytest

# Import the standalone script by path (it lives in scripts/, not on sys.path).
_SCRIPT = Path(__file__).resolve().parents[1] / "scale_stl.py"
_spec = importlib.util.spec_from_file_location("scale_stl", _SCRIPT)
scale_stl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scale_stl)

Triangle = scale_stl.Triangle


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _one_triangle():
    return Triangle(
        normal=(0.0, 0.0, 1.0),
        v1=(0.0, 0.0, 0.0),
        v2=(1.0, 0.0, 0.0),
        v3=(0.0, 1.0, 0.0),
        attr=0,
    )


def _write_binary_stl(path: Path, triangles):
    path.write_bytes(scale_stl.serialize_binary_stl(b"test header", triangles))


_ASCII_STL = (
    "solid test\n"
    "  facet normal 0 0 1\n"
    "    outer loop\n"
    "      vertex 0 0 0\n"
    "      vertex 1 0 0\n"
    "      vertex 0 1 0\n"
    "    endloop\n"
    "  endfacet\n"
    "endsolid test\n"
)


# --------------------------------------------------------------------------- #
# Binary parse/serialize
# --------------------------------------------------------------------------- #

def test_binary_stl_roundtrip():
    tris = [_one_triangle()]
    data = scale_stl.serialize_binary_stl(b"hello", tris)
    header, parsed = scale_stl.parse_binary_stl(data)
    assert header[:5] == b"hello"
    assert len(header) == 80
    assert parsed == tris


def test_parse_binary_stl_raises_on_truncated_data():
    data = scale_stl.serialize_binary_stl(b"", [_one_triangle(), _one_triangle()])
    truncated = data[:-10]
    with pytest.raises(ValueError):
        scale_stl.parse_binary_stl(truncated)


def test_is_binary_stl_detects_binary_by_declared_size(tmp_path):
    p = tmp_path / "a.stl"
    _write_binary_stl(p, [_one_triangle()])
    assert scale_stl.is_binary_stl(p) is True


def test_is_binary_stl_detects_ascii_by_size_mismatch(tmp_path):
    p = tmp_path / "a.stl"
    p.write_text(_ASCII_STL)
    assert scale_stl.is_binary_stl(p) is False


def test_is_binary_stl_handles_tiny_file(tmp_path):
    p = tmp_path / "a.stl"
    p.write_bytes(b"nope")
    assert scale_stl.is_binary_stl(p) is False


# --------------------------------------------------------------------------- #
# scale_triangles
# --------------------------------------------------------------------------- #

def test_scale_triangles_scales_vertices_only():
    t = _one_triangle()
    [scaled] = scale_stl.scale_triangles([t], 2.0)
    assert scaled.v1 == (0.0, 0.0, 0.0)
    assert scaled.v2 == (2.0, 0.0, 0.0)
    assert scaled.v3 == (0.0, 2.0, 0.0)
    assert scaled.normal == t.normal
    assert scaled.attr == t.attr


# --------------------------------------------------------------------------- #
# ASCII parsing
# --------------------------------------------------------------------------- #

def test_parse_ascii_stl_reads_triangle():
    [t] = scale_stl.parse_ascii_stl(_ASCII_STL)
    assert t.normal == (0.0, 0.0, 1.0)
    assert t.v1 == (0.0, 0.0, 0.0)
    assert t.v2 == (1.0, 0.0, 0.0)
    assert t.v3 == (0.0, 1.0, 0.0)
    assert t.attr == 0


def test_parse_ascii_stl_reads_multiple_solid_blocks():
    text = _ASCII_STL + _ASCII_STL
    triangles = scale_stl.parse_ascii_stl(text)
    assert len(triangles) == 2


def test_parse_ascii_stl_case_and_whitespace_tolerant():
    text = _ASCII_STL.upper()
    triangles = scale_stl.parse_ascii_stl(text)
    assert len(triangles) == 1


def test_parse_ascii_stl_raises_on_no_facets():
    with pytest.raises(ValueError):
        scale_stl.parse_ascii_stl("solid empty\nendsolid empty\n")


# --------------------------------------------------------------------------- #
# scale_stl_file — always writes binary, regardless of input format
# --------------------------------------------------------------------------- #

def test_scale_stl_file_binary_input(tmp_path):
    src = tmp_path / "in.stl"
    _write_binary_stl(src, [_one_triangle()])
    dest = tmp_path / "out.stl"

    scale_stl.scale_stl_file(src, dest, 2.0)

    assert scale_stl.is_binary_stl(dest)
    _, [t] = scale_stl.parse_binary_stl(dest.read_bytes())
    assert t.v2 == (2.0, 0.0, 0.0)


def test_scale_stl_file_ascii_input_writes_binary_output(tmp_path):
    src = tmp_path / "in.stl"
    src.write_text(_ASCII_STL)
    dest = tmp_path / "out.stl"

    scale_stl.scale_stl_file(src, dest, 2.0)

    assert scale_stl.is_binary_stl(dest)
    _, [t] = scale_stl.parse_binary_stl(dest.read_bytes())
    assert t.v2 == (2.0, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# find_stl_files / naming / collect_inputs
# --------------------------------------------------------------------------- #

def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def test_find_stl_files_top_level_only(tmp_path):
    _touch(tmp_path / "a.stl")
    _touch(tmp_path / "b.STL")
    _touch(tmp_path / "c.txt")
    _touch(tmp_path / "sub" / "d.stl")

    found = scale_stl.find_stl_files(tmp_path, recursive=False)
    assert found == [tmp_path / "a.stl", tmp_path / "b.STL"]


def test_find_stl_files_recursive(tmp_path):
    _touch(tmp_path / "a.stl")
    _touch(tmp_path / "sub" / "d.stl")

    found = scale_stl.find_stl_files(tmp_path, recursive=True)
    assert found == [tmp_path / "a.stl", tmp_path / "sub" / "d.stl"]


def test_prefixed_name():
    assert scale_stl.prefixed_name(Path("hex_q00_r00.stl"), "10mm_") == "10mm_hex_q00_r00.stl"


def test_output_path_for():
    out = scale_stl.output_path_for(Path("/out"), Path("/in/a.stl"), "10mm_")
    assert out == Path("/out/10mm_a.stl")


def test_collect_inputs_single_file(tmp_path):
    f = tmp_path / "a.stl"
    _touch(f)
    assert scale_stl.collect_inputs(f, recursive=False) == [f]


def test_collect_inputs_directory(tmp_path):
    _touch(tmp_path / "a.stl")
    _touch(tmp_path / "b.stl")
    assert scale_stl.collect_inputs(tmp_path, recursive=False) == [
        tmp_path / "a.stl", tmp_path / "b.stl"
    ]


def test_collect_inputs_rejects_non_stl_file(tmp_path):
    f = tmp_path / "a.txt"
    _touch(f)
    with pytest.raises(ValueError):
        scale_stl.collect_inputs(f, recursive=False)


def test_collect_inputs_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scale_stl.collect_inputs(tmp_path / "missing.stl", recursive=False)


# --------------------------------------------------------------------------- #
# run() end-to-end
# --------------------------------------------------------------------------- #

def test_run_scales_directory_with_prefix(tmp_path):
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    _write_binary_stl(in_dir / "a.stl", [_one_triangle()])
    (in_dir / "b.stl").write_text(_ASCII_STL)

    code = scale_stl.run(in_dir, out_dir, 2.0, "10mm_", force=False, recursive=False)

    assert code == 0
    assert (out_dir / "10mm_a.stl").exists()
    assert (out_dir / "10mm_b.stl").exists()


def test_run_creates_output_dir_if_missing(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    _write_binary_stl(in_dir / "a.stl", [_one_triangle()])
    out_dir = tmp_path / "nested" / "out"

    code = scale_stl.run(in_dir, out_dir, 1.0, "x_", force=False, recursive=False)

    assert code == 0
    assert (out_dir / "x_a.stl").exists()


def test_run_skips_existing_output_without_force(tmp_path):
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    _write_binary_stl(in_dir / "a.stl", [_one_triangle()])
    (out_dir / "x_a.stl").write_bytes(b"existing")

    code = scale_stl.run(in_dir, out_dir, 1.0, "x_", force=False, recursive=False)

    assert code == 1
    assert (out_dir / "x_a.stl").read_bytes() == b"existing"


def test_run_overwrites_with_force(tmp_path):
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    _write_binary_stl(in_dir / "a.stl", [_one_triangle()])
    (out_dir / "x_a.stl").write_bytes(b"existing")

    code = scale_stl.run(in_dir, out_dir, 1.0, "x_", force=True, recursive=False)

    assert code == 0
    assert scale_stl.is_binary_stl(out_dir / "x_a.stl")


def test_run_rejects_zero_or_negative_scale(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out_dir = tmp_path / "out"

    assert scale_stl.run(in_dir, out_dir, 0.0, "x_", force=False, recursive=False) == 2
    assert scale_stl.run(in_dir, out_dir, -1.0, "x_", force=False, recursive=False) == 2


def test_run_empty_directory_returns_zero_and_writes_nothing(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out_dir = tmp_path / "out"

    code = scale_stl.run(in_dir, out_dir, 1.0, "x_", force=False, recursive=False)

    assert code == 0
    assert not out_dir.exists()


def test_run_missing_input_returns_setup_error(tmp_path):
    code = scale_stl.run(
        tmp_path / "missing", tmp_path / "out", 1.0, "x_", force=False, recursive=False
    )
    assert code == 2


# --------------------------------------------------------------------------- #
# Mandated worked-example factors (28mm <-> 10mm)
# --------------------------------------------------------------------------- #

def test_scale_28mm_to_10mm_factor():
    t = Triangle((0, 0, 1), (28.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0)
    [scaled] = scale_stl.scale_triangles([t], 0.357)
    assert scaled.v1[0] == pytest.approx(10.0, abs=1e-2)


def test_scale_10mm_to_28mm_factor():
    t = Triangle((0, 0, 1), (10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0)
    [scaled] = scale_stl.scale_triangles([t], 2.8)
    assert scaled.v1[0] == pytest.approx(28.0)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def test_main_end_to_end(tmp_path):
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    _write_binary_stl(in_dir / "a.stl", [_one_triangle()])

    code = scale_stl.main(
        [str(in_dir), "0.357", "--prefix", "10mm_", "-o", str(out_dir)]
    )

    assert code == 0
    assert (out_dir / "10mm_a.stl").exists()
