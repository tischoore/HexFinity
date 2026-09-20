#!/usr/bin/env python3
"""Batch-scale STL files uniformly, writing prefixed copies to an output dir.

Standalone CPython script (no ``bpy``) — run it directly with any Python 3:

    python scripts/scale_stl.py <input> <scale> --prefix PREFIX -o OUTPUT_DIR

``<input>`` is a single ``.stl`` file or a directory of them (non-recursive
unless ``-r``/``--recursive`` is given). ``<scale>`` is the uniform scale
factor applied to every vertex. Every output filename is
``"<prefix><original name>"`` written under ``OUTPUT_DIR`` (created if
missing). An existing output file is skipped (not overwritten) unless
``--force`` is given.

Both binary and ASCII STL input are accepted, but **output is always written
binary** — matching the STL flavour Blender's own ``wm.stl_export`` produces
elsewhere in this repo, and simpler/smaller than round-tripping ASCII text.

The pure helpers below (everything except the CLI glue) are import-safe and
covered by ``scripts/tests/test_scale_stl.py``.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from collections import namedtuple
from pathlib import Path

Triangle = namedtuple("Triangle", "normal v1 v2 v3 attr")

_HEADER_SIZE = 80
_TRIANGLE_STRUCT = struct.Struct("<12fH")  # normal + v1 + v2 + v3 (12 floats) + attr


# --------------------------------------------------------------------------- #
# Pure, in-memory transforms — bpy-free, no disk I/O.
# --------------------------------------------------------------------------- #

def is_binary_stl(path: Path) -> bool:
    """True if `path` looks like a binary STL: its size matches exactly what
    the 80-byte header + declared triangle count predicts. (The same
    heuristic other STL tooling uses — an ASCII file's size coincidentally
    matching this formula is a known, accepted, extremely unlikely edge
    case.)"""
    size = path.stat().st_size
    if size < _HEADER_SIZE + 4:
        return False
    with open(path, "rb") as f:
        f.seek(_HEADER_SIZE)
        count_bytes = f.read(4)
    (count,) = struct.unpack("<I", count_bytes)
    return size == _HEADER_SIZE + 4 + _TRIANGLE_STRUCT.size * count


def parse_binary_stl(data: bytes) -> tuple[bytes, list[Triangle]]:
    """Parse a binary STL byte string into (header, triangles). Raises
    `ValueError` if `data` is shorter than the declared triangle count
    requires (truncated/corrupt file)."""
    if len(data) < _HEADER_SIZE + 4:
        raise ValueError("file too short to be a binary STL")
    header = data[:_HEADER_SIZE]
    (count,) = struct.unpack_from("<I", data, _HEADER_SIZE)
    offset = _HEADER_SIZE + 4
    needed = offset + _TRIANGLE_STRUCT.size * count
    if len(data) < needed:
        raise ValueError(
            f"truncated binary STL: declares {count} triangle(s), "
            f"needs {needed} bytes, has {len(data)}"
        )
    triangles = []
    for i in range(count):
        nx, ny, nz, x1, y1, z1, x2, y2, z2, x3, y3, z3, attr = (
            _TRIANGLE_STRUCT.unpack_from(data, offset + i * _TRIANGLE_STRUCT.size)
        )
        triangles.append(
            Triangle((nx, ny, nz), (x1, y1, z1), (x2, y2, z2), (x3, y3, z3), attr)
        )
    return header, triangles


def serialize_binary_stl(header: bytes, triangles: list[Triangle]) -> bytes:
    """Inverse of `parse_binary_stl`. `header` is padded with zero bytes (or
    truncated) to exactly 80 bytes."""
    header = (header[:_HEADER_SIZE]).ljust(_HEADER_SIZE, b"\0")
    out = bytearray(header)
    out += struct.pack("<I", len(triangles))
    for t in triangles:
        out += _TRIANGLE_STRUCT.pack(
            *t.normal, *t.v1, *t.v2, *t.v3, t.attr
        )
    return bytes(out)


def scale_triangles(triangles: list[Triangle], factor: float) -> list[Triangle]:
    """Scale every vertex of every triangle by `factor`. `normal` and `attr`
    are copied through unchanged — valid only for a *uniform* scale factor,
    since uniform scaling never changes a normal's direction, only the
    magnitude of position vectors. Do not reuse this for non-uniform
    (per-axis) scaling."""
    return [
        Triangle(
            t.normal,
            tuple(c * factor for c in t.v1),
            tuple(c * factor for c in t.v2),
            tuple(c * factor for c in t.v3),
            t.attr,
        )
        for t in triangles
    ]


_FACET_RE = re.compile(
    r"facet\s+normal\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)\s+"
    r"outer\s+loop\s+"
    r"vertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)\s+"
    r"vertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)\s+"
    r"vertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)\s+"
    r"endloop\s+"
    r"endfacet",
    re.IGNORECASE,
)


def parse_ascii_stl(text: str) -> list[Triangle]:
    """Parse an ASCII STL's `facet normal ... vertex ... endfacet` blocks
    into triangles (any `solid`/`endsolid` names, comments, or multiple
    concatenated `solid` blocks are ignored — every facet found anywhere in
    the text is collected). `attr` is always 0 (ASCII STL has no attribute
    field). Raises `ValueError` if no facets are found."""
    triangles = []
    for m in _FACET_RE.finditer(text):
        nums = [float(g) for g in m.groups()]
        normal = tuple(nums[0:3])
        v1 = tuple(nums[3:6])
        v2 = tuple(nums[6:9])
        v3 = tuple(nums[9:12])
        triangles.append(Triangle(normal, v1, v2, v3, 0))
    if not triangles:
        raise ValueError("no facets found in ASCII STL")
    return triangles


# --------------------------------------------------------------------------- #
# Thin path-based glue — disk I/O, still bpy-free.
# --------------------------------------------------------------------------- #

def find_stl_files(root: Path, recursive: bool) -> list[Path]:
    """Sorted `.stl` files directly under `root` (or recursively under it),
    matched case-insensitively so `.STL` is found too."""
    it = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() == ".stl")


def prefixed_name(src: Path, prefix: str) -> str:
    """The output filename for `src`: `prefix` prepended verbatim."""
    return f"{prefix}{src.name}"


def output_path_for(output_dir: Path, src: Path, prefix: str) -> Path:
    return output_dir / prefixed_name(src, prefix)


def collect_inputs(input_path: Path, recursive: bool) -> list[Path]:
    """Resolve `input_path` (a single `.stl` file or a directory) to the
    list of `.stl` files to process. Raises `FileNotFoundError` if it
    doesn't exist, `ValueError` if it's a file but not `.stl`."""
    if not input_path.exists():
        raise FileNotFoundError(f"not found: {input_path}")
    if input_path.is_dir():
        return find_stl_files(input_path, recursive)
    if input_path.suffix.lower() != ".stl":
        raise ValueError(f"not an .stl file: {input_path}")
    return [input_path]


def scale_stl_file(src: Path, dest: Path, factor: float) -> None:
    """Scale one STL file (binary or ASCII input) into `dest`, always
    written as binary STL."""
    data = src.read_bytes()
    if is_binary_stl(src):
        header, triangles = parse_binary_stl(data)
    else:
        triangles = parse_ascii_stl(data.decode("utf-8"))
        header = b""
    scaled = scale_triangles(triangles, factor)
    dest.write_bytes(serialize_binary_stl(header, scaled))


# --------------------------------------------------------------------------- #
# Orchestration + CLI.
# --------------------------------------------------------------------------- #

def run(
    input_path: Path,
    output_dir: Path,
    factor: float,
    prefix: str,
    force: bool,
    recursive: bool,
) -> int:
    if factor <= 0:
        print(f"ERROR: scale must be a positive number, got {factor}")
        return 2

    try:
        files = collect_inputs(input_path, recursive)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2

    if not files:
        print(f"No .stl files found under {input_path}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)

    ok = 0
    for f in files:
        dest = output_path_for(output_dir, f, prefix)
        print(f"{f} -> {dest}")
        if dest.exists() and not force:
            print("  SKIPPED: output already exists (use --force to overwrite)")
            continue
        try:
            scale_stl_file(f, dest, factor)
        except ValueError as exc:
            print(f"  SKIPPED: {exc}")
            continue
        ok += 1

    failed = len(files) - ok
    print(
        f"\n{ok}/{len(files)} file(s) scaled"
        + (f", {failed} skipped" if failed else "")
    )
    return 0 if failed == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="scale_stl.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("input", help="An .stl file or a folder of .stl files.")
    parser.add_argument("scale", type=float, help="Uniform scale factor (must be > 0).")
    parser.add_argument(
        "--prefix", required=True,
        help="Prefix prepended to each output filename, e.g. '10mm_'.",
    )
    parser.add_argument(
        "-o", "--output", required=True, dest="output_dir",
        help="Output folder for the scaled copies (created if missing).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing output file instead of skipping it.",
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="When input is a folder, recurse into subfolders (default: top level only).",
    )
    args = parser.parse_args(argv)

    return run(
        Path(args.input),
        Path(args.output_dir),
        args.scale,
        args.prefix,
        args.force,
        args.recursive,
    )


if __name__ == "__main__":
    sys.exit(main())
