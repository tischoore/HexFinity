"""Headless smoke test: register the extension, generate a minimal one-tile
map, and exercise hexfinity.export_and_slice end to end — STL export via the
shared _export_tiles_core/_iter_export_tiles, then a real Bambu Studio CLI
slice (if installed), producing paired .gcode/.gcode.3mf files, and the
delete-STL-on-success behaviour. Also confirms hexfinity.export_tiles (the
original button) still behaves identically after the _export_tiles_core
refactor, and exercises the Printer/Nozzle/Filament/Quality dropdown
machinery (items= callbacks, cascading resets, and settings-JSON
persistence).

Run with:
    blender --background --python tests/_headless_export_and_slice_check.py
Exits non-zero on failure (raises) so it can gate CI / manual checks.

If Bambu Studio isn't installed on this machine, the slicing/dropdown
portions are skipped with a clear message — the STL-export/refactor checks
still run.

Both operators are now _ExportProgressMixin modal operators backing an
interactive progress popup with a Cancel button (see operators.py's
_start_progress). `bpy.app.background` is True for this whole script
(`--background`), so _start_progress takes its synchronous fallback branch —
there is no windowing event loop in background mode to deliver TIMER events
to a modal operator/invoke_popup, so the chunked/popup/Cancel path is
unreachable here by design; both operators instead drain their generator in
one call and return {'FINISHED'}/{'CANCELLED'} immediately, same as before
this feature existed. That's exactly what this script continues to check.
The popup/spinner/Cancel-button UX itself (including mid-export and
mid-slice cancellation) can only be exercised by hand in interactive
Blender — generate a map, click Export Tiles to STL or Export + Slice, and
confirm: the popup appears and shows live tile/job progress; the Cancel
button, Esc, and dismissing the popup by clicking elsewhere all stop the
operation (immediately between tiles while exporting; after the in-flight
job finishes while slicing, per the confirmed cooperative-cancellation
design); a cancelled export still writes a manifest covering whatever
completed; normal completion reports the same messages as before.

IMPORTANT: the persistence checks below monkeypatch
bambu_slicer.blender_settings_path() to point at a throwaway temp file
instead of the real (git-tracked!) scripts/slice_tiles_settings.json, so
running this script never dirties that file. This ONLY works reliably if
there is a single active "hexfinity" module tree — if this machine also has
the extension enabled via a dev junction (deploy.ps1 -Dev), Blender
auto-loads it as a SEPARATE module tree (bl_ext.user_default.hexfinity,
resolving through the junction to the same real files), whose own unrelated
bambu_slicer module instance would not see this script's monkeypatch and
would happily write straight to the real settings file. Disable that
auto-loaded copy first so only this script's plain `import hexfinity` is
ever active.
"""
import os
import sys
import tempfile

import bpy

assert bpy.app.background, (
    "this script must run via `blender --background ...` — it relies on "
    "_ExportProgressMixin._start_progress's bpy.app.background fallback to "
    "drain export/slice synchronously instead of starting a popup/timer")

try:
    bpy.ops.preferences.addon_disable(module="bl_ext.user_default.hexfinity")
except Exception:
    pass

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
# Import as hexfinity.bambu_slicer (NOT a bare `import bambu_slicer` via a
# sys.path insertion) so this is the exact same module object operators.py/
# properties.py's own `from . import bambu_slicer` resolve to — monkeypatching
# a separately-loaded copy below would silently have no effect on the real
# code path.
from hexfinity import bambu_slicer, operators  # noqa: E402

exe = bambu_slicer.find_bambu_executable()
if not exe:
    print("Bambu Studio not found on this machine — skipping slice checks.")
else:
    print("Bambu Studio found:", exe)
    slice_props = scene.hexfinity_slice
    slice_props.delete_stls_after_slicing = True
    slice_props.adaptive_layer_height = True

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

    with open(os.path.join(export_dir, gcodes[0]), encoding="utf-8") as fh:
        gcode_text = fh.read()
    assert "adaptive_layer_height = 1" in gcode_text, (
        "expected adaptive_layer_height = 1 in the sliced G-code config "
        "block when adaptive_layer_height=True", gcodes[0])
    print("adaptive_layer_height override reached the slicer OK")
    slice_props.adaptive_layer_height = False

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

    # ---- dropdown items= callbacks, cascading reset, settings-JSON --------
    # persistence. Redirect persistence at a throwaway temp file BEFORE
    # touching printer/nozzle/filament/quality at all (each is a property
    # write that fires its own update= callback, which would otherwise
    # persist straight to the real (git-tracked!) scripts/
    # slice_tiles_settings.json).
    from hexfinity import properties
    settings_dir = tempfile.mkdtemp(prefix="hf_slice_settings_")
    settings_path = os.path.join(settings_dir, "slice_tiles_settings.json")
    real_path_fn = bambu_slicer.blender_settings_path
    bambu_slicer.blender_settings_path = lambda base=None: settings_path
    try:
        slice_props.printer = properties._SLICE_NONE_ID
        for fn in (properties._slice_printer_items, properties._slice_nozzle_items,
                   properties._slice_filament_items, properties._slice_quality_items):
            items = fn(slice_props, bpy.context)
            assert items and items[0][0] == properties._SLICE_NONE_ID, (fn.__name__, items)
            assert all(len(it) == 3 for it in items), (fn.__name__, items)
        print("dropdown items= callbacks OK")

        printers = sorted(bambu_slicer.cached_printers(
            bambu_slicer.profiles_dir(exe)))
        assert printers, "expected at least one installed printer"
        real_printer = printers[0]

        slice_props.nozzle = properties._SLICE_NONE_ID
        slice_props.filament = properties._SLICE_NONE_ID
        slice_props.quality = properties._SLICE_NONE_ID
        slice_props.printer = real_printer
        assert slice_props.nozzle == properties._SLICE_NONE_ID, \
            "printer change must reset nozzle"
        assert slice_props.filament == properties._SLICE_NONE_ID, \
            "printer change must cascade-reset filament"
        assert slice_props.quality == properties._SLICE_NONE_ID, \
            "printer change must cascade-reset quality"

        persisted = bambu_slicer.load_settings(settings_path)
        assert persisted["printer"] == real_printer, persisted
        assert persisted["nozzle"] == "", persisted  # "NONE" id -> "" on disk
        print("cascade reset + persistence OK:", persisted["printer"])

        slice_props.adaptive_layer_height = True
        persisted = bambu_slicer.load_settings(settings_path)
        assert persisted["adaptive_layer_height"] is True, persisted
        print("adaptive_layer_height persistence OK")

        # Re-seeding from the settings file must pick the value back up.
        # Clear the in-memory value WITHOUT persisting the reset (mirrors
        # _seed_from_settings_file's own FILLING-guarded assignments) so
        # this genuinely proves a read from disk, not leftover state.
        properties._SLICE_FILLING = True
        try:
            slice_props.printer = properties._SLICE_NONE_ID
        finally:
            properties._SLICE_FILLING = False
        operators.HEXFINITY_OT_export_and_slice._seed_from_settings_file(bpy.context)
        assert slice_props.printer == real_printer, slice_props.printer
        print("seed-from-settings-file OK")
    finally:
        bambu_slicer.blender_settings_path = real_path_fn

print("ALL CHECKS PASSED")
