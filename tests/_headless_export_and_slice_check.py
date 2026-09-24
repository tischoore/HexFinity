"""Headless smoke test: register the extension, generate a minimal one-tile
map, and exercise hexfinity.export_and_slice end to end — STL export via the
shared _export_tiles_core, then a real Bambu Studio CLI slice (if installed),
producing paired .gcode/.gcode.3mf files, and the delete-STL-on-success
behaviour. Also confirms hexfinity.export_tiles (the original button) still
behaves identically after the _export_tiles_core refactor.

Run with:
    blender --background --python tests/_headless_export_and_slice_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.

If Bambu Studio isn't installed on this machine, the slicing portion is
skipped with a clear message — the STL-export/refactor checks still run.
"""
import os
import sys
import tempfile

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import hexfinity

hexfinity.register()
print("register() OK")

scene = bpy.context.scene
mp = scene.hexfinity_map
mp.diameter_mm = 220.0
mp.level_height_mm = 10.0
mp.base_thickness_mm = 10.0
mp.smoothness_passes = 2
mp.resample_density = 0
mp.man_height_mm = 10.0
mp.grid_x = 0
mp.grid_y = 0

res = bpy.ops.hexfinity.generate_map()
assert res == {'FINISHED'}, res
print("generate_map() OK")

# ---- hexfinity.export_tiles must still behave identically ----------------
out_dir_plain = tempfile.mkdtemp(prefix="hf_export_only_")
res = bpy.ops.hexfinity.export_tiles(
    'EXEC_DEFAULT', directory=out_dir_plain, subfolder="out")
assert res == {'FINISHED'}, res
plain_files = os.listdir(os.path.join(out_dir_plain, "out"))
assert any(f.endswith(".stl") for f in plain_files), plain_files
assert "manifest.json" in plain_files, plain_files
print("export_tiles (unchanged) OK:", plain_files)

# ---- hexfinity.export_and_slice ------------------------------------------
sys.path.insert(0, os.path.join(REPO, "hexfinity"))
import bambu_slicer  # noqa: E402

exe = bambu_slicer.find_bambu_executable()
if not exe:
    print("Bambu Studio not found on this machine — skipping slice checks.")
else:
    print("Bambu Studio found:", exe)
    slice_props = scene.hexfinity_slice
    slice_props.delete_stls_after_slicing = True

    out_dir = tempfile.mkdtemp(prefix="hf_export_and_slice_")
    res = bpy.ops.hexfinity.export_and_slice(
        'EXEC_DEFAULT', directory=out_dir, subfolder="out")
    assert res == {'FINISHED'}, res

    export_dir = os.path.join(out_dir, "out")
    files = sorted(os.listdir(export_dir))
    print("export_and_slice produced:", files)

    stls = [f for f in files if f.endswith(".stl")]
    gcodes = [f for f in files if f.endswith(".gcode")]
    threemfs = [f for f in files if f.endswith(".gcode.3mf")]
    assert not stls, ("STL(s) should have been deleted after a successful "
                      "slice (delete_stls_after_slicing=True)", files)
    assert gcodes, ("expected at least one .gcode file", files)
    assert threemfs, ("expected at least one .gcode.3mf file", files)
    assert "manifest.json" in files, files
    print("export_and_slice (delete on success) OK")

    # ---- re-run with the checkbox off: STL(s) must be kept ----------------
    out_dir2 = tempfile.mkdtemp(prefix="hf_export_and_slice_keep_")
    slice_props.delete_stls_after_slicing = False
    res = bpy.ops.hexfinity.export_and_slice(
        'EXEC_DEFAULT', directory=out_dir2, subfolder="out")
    assert res == {'FINISHED'}, res
    files2 = sorted(os.listdir(os.path.join(out_dir2, "out")))
    assert any(f.endswith(".stl") for f in files2), (
        "STL(s) should be kept when delete_stls_after_slicing=False", files2)
    print("export_and_slice (keep STLs) OK:", files2)

print("ALL CHECKS PASSED")
