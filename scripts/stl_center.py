#!/usr/bin/env python3
"""Batch re-center STL origins to their bounding-box center (or bottom).

Unlike `scripts/slice_tiles.py`, this is **not** a plain-CPython script — it
uses `bpy.ops.wm.stl_import` / `bpy.ops.wm.stl_export`, the same STL I/O
already used by `hexfinity/operators.py` and `hexfinity/flora.py`, so it must
run inside Blender:

    "C:\\Program Files\\Blender Foundation\\Blender 5.1\\blender.exe" ^
        --background --python scripts\\stl_center.py -- <folder> [-r] [-b]

Blender does not strip its own flags from `sys.argv` before handing control
to the script — `sys.argv` here is the *entire* command line, including
`--background --python ...`. `main()` finds the `--` itself and only parses
what comes after it, the standard idiom for Blender command-line scripts.

For every `.stl` under `<folder>` (optionally recursive via `-r`), the mesh
is translated — never rotated or scaled — so its origin lands at the
geometric center of its bounding box in X and Y, and by default in Z too.
Pass `-b`/`--bottom` to instead put the origin's Z at the mesh's lowest
point (Z is assumed to point up), useful for assets meant to sit flush on a
surface. Each file is overwritten in place.

The pure helpers below (`find_stl_files`, `compute_offset`) don't touch
`bpy` and are unit-tested in `scripts/tests/test_stl_center.py`; the actual
import/translate/export glue (`center_stl`) has no automated coverage, the
same convention already accepted in this repo for other bpy-only code
(`regions.py`, `scatter.py`) — verify it by hand instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import bpy
    from mathutils import Vector, Matrix
except ImportError:
    bpy = None


# --------------------------------------------------------------------------- #
# Pure helpers — bpy-free, unit-testable.
# --------------------------------------------------------------------------- #

def find_stl_files(root: Path, recursive: bool) -> list[Path]:
    """Sorted `.stl` files directly under `root` (or recursively under it),
    matched case-insensitively so `.STL` is found too."""
    it = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() == ".stl")


def compute_offset(bmin, bmax, bottom: bool) -> tuple[float, float, float]:
    """The point (in the mesh's own local space) that should become the new
    origin: the bounding-box center in X/Y, and in Z either the box center
    (default) or its minimum (`bottom=True`)."""
    cx = (bmin[0] + bmax[0]) / 2.0
    cy = (bmin[1] + bmax[1]) / 2.0
    cz = bmin[2] if bottom else (bmin[2] + bmax[2]) / 2.0
    return (cx, cy, cz)


# --------------------------------------------------------------------------- #
# bpy glue.
# --------------------------------------------------------------------------- #

def center_stl(filepath: Path, bottom: bool) -> bool:
    """Re-center one STL file in place. Returns True on success, False if it
    was skipped (import failure or produced no geometry) — logged either way."""
    scene = bpy.context.scene
    before = set(scene.objects)
    try:
        bpy.ops.wm.stl_import(filepath=str(filepath))
    except RuntimeError as exc:
        print(f"  SKIPPED: import failed: {exc}")
        return False

    imported = [o for o in scene.objects if o not in before]
    if not imported:
        print("  SKIPPED: import produced no objects")
        return False

    bmin = [float("inf")] * 3
    bmax = [float("-inf")] * 3
    for obj in imported:
        for v in obj.data.vertices:
            for i in range(3):
                bmin[i] = min(bmin[i], v.co[i])
                bmax[i] = max(bmax[i], v.co[i])

    if bmin[0] > bmax[0]:
        print("  SKIPPED: no vertices found")
        for obj in imported:
            _remove_object(obj)
        return False

    offset = compute_offset(tuple(bmin), tuple(bmax), bottom)
    translation = Matrix.Translation(-Vector(offset))
    for obj in imported:
        obj.data.transform(translation)

    for o in scene.objects:
        o.select_set(False)
    for obj in imported:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = imported[0]

    try:
        bpy.ops.wm.stl_export(
            filepath=str(filepath),
            export_selected_objects=True,
            apply_modifiers=True,
        )
    except RuntimeError as exc:
        print(f"  SKIPPED: export failed: {exc}")
        for obj in imported:
            _remove_object(obj)
        return False

    for obj in imported:
        _remove_object(obj)
    return True


def _remove_object(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def main(argv=None):
    if bpy is None:
        print("ERROR: stl_center.py needs bpy — run it inside Blender:\n"
              '  "C:\\Program Files\\Blender Foundation\\Blender 5.1\\blender.exe" '
              "--background --python scripts\\stl_center.py -- <folder> [-r] [-b]")
        return 2

    if argv is None:
        # Blender hands the script the full command line, not just the part
        # after `--` — find it ourselves (standard Blender script idiom).
        argv = sys.argv[1:]
        if "--" in argv:
            argv = argv[argv.index("--") + 1:]

    parser = argparse.ArgumentParser(
        prog="stl_center.py", description=__doc__.splitlines()[0])
    parser.add_argument("folder", help="Folder of .stl files to re-center.")
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="Recurse into subfolders (default: top level only).",
    )
    parser.add_argument(
        "-b", "--bottom", action="store_true",
        help="Place the new origin's Z at the mesh's lowest point instead "
             "of the bounding-box center.",
    )
    args = parser.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"ERROR: not a folder: {folder}")
        return 2

    files = find_stl_files(folder, args.recursive)
    if not files:
        print(f"No .stl files found under {folder}")
        return 0

    ok = 0
    for f in files:
        print(f"{f}")
        if center_stl(f, args.bottom):
            ok += 1

    failed = len(files) - ok
    print(f"\n{ok}/{len(files)} file(s) re-centered"
          + (f", {failed} skipped" if failed else ""))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
