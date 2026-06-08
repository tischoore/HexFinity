"""Tests for the bpy-free logic in ``scripts/slice_tiles.py``.

These exercise the pure helpers only — no tkinter, no real Bambu Studio call —
so they run under any CPython (including Blender's bundled interpreter):

    python -m pytest scripts/tests -v
"""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

# Import the standalone script by path (it lives in scripts/, not on sys.path).
_SCRIPT = Path(__file__).resolve().parents[1] / "slice_tiles.py"
_spec = importlib.util.spec_from_file_location("slice_tiles", _SCRIPT)
slice_tiles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slice_tiles)


# --------------------------------------------------------------------------- #
# tile_counts / count_for
# --------------------------------------------------------------------------- #

def test_tile_counts_groups_by_file():
    rows = [
        {"q": 0, "r": 0, "file": "hex_q00_r00.stl", "custom": False},
        {"q": 1, "r": 0, "file": "hex_q00_r00.stl", "custom": False},
        {"q": 2, "r": 0, "file": "hex_q00_r00.stl", "custom": False},
        {"q": 0, "r": 1, "file": "hex_q00_r01_abcd1234.stl", "custom": True},
    ]
    counts = slice_tiles.tile_counts(rows)
    assert counts == {"hex_q00_r00.stl": 3, "hex_q00_r01_abcd1234.stl": 1}


def test_count_for_defaults_to_one_when_absent():
    counts = {"hex_q00_r00.stl": 4}
    assert slice_tiles.count_for(counts, "hex_q00_r00.stl") == 4
    assert slice_tiles.count_for(counts, "not_in_manifest.stl") == 1


def test_tile_counts_ignores_rows_without_file():
    assert slice_tiles.tile_counts([{"q": 0, "r": 0}]) == {}


def test_read_manifest_missing_returns_empty(tmp_path):
    assert slice_tiles.read_manifest(tmp_path) == []


def test_read_manifest_roundtrip(tmp_path):
    rows = [{"q": 0, "r": 0, "file": "a.stl", "custom": False}]
    (tmp_path / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    assert slice_tiles.read_manifest(tmp_path) == rows


# --------------------------------------------------------------------------- #
# Output naming
# --------------------------------------------------------------------------- #

def test_gcode_name_puts_count_before_extension():
    assert slice_tiles.gcode_name("hex_q00_r00", 4) == "hex_q00_r00.4.gcode"


def test_threemf_name():
    assert slice_tiles.threemf_name("hex_q00_r00", 4) == "hex_q00_r00.4.gcode.3mf"


# --------------------------------------------------------------------------- #
# Inheritance flattening
# --------------------------------------------------------------------------- #

def _write_profile(root, category, name, data):
    data = dict(data)
    data.setdefault("name", name)
    path = Path(root) / category / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_flatten_resolves_inherits_chain_with_child_override(tmp_path):
    _write_profile(tmp_path, "process", "base", {
        "sparse_infill_density": "15%",
        "sparse_infill_pattern": "grid",
        "layer_height": "0.2",
    })
    _write_profile(tmp_path, "process", "child", {
        "inherits": "base",
        "sparse_infill_pattern": "gyroid",  # override parent
    })
    index = slice_tiles.build_profile_index(tmp_path)
    flat = slice_tiles.flatten_profile(index, "process", "child")

    assert flat["sparse_infill_density"] == "15%"   # inherited
    assert flat["sparse_infill_pattern"] == "gyroid"  # overridden
    assert flat["layer_height"] == "0.2"
    assert "inherits" not in flat


def test_flatten_merges_machine_include(tmp_path):
    _write_profile(tmp_path, "machine", "common", {"bed_size": "256"})
    _write_profile(tmp_path, "machine", "start_gcode_tpl",
                   {"machine_start_gcode": "G28"})
    _write_profile(tmp_path, "machine", "p1s", {
        "inherits": "common",
        "include": ["start_gcode_tpl"],
        "nozzle_diameter": ["0.4"],
    })
    index = slice_tiles.build_profile_index(tmp_path)
    flat = slice_tiles.flatten_profile(index, "machine", "p1s")

    assert flat["bed_size"] == "256"                 # from inherits
    assert flat["machine_start_gcode"] == "G28"      # from include
    assert flat["nozzle_diameter"] == ["0.4"]        # own
    assert "include" not in flat


def test_flatten_cycle_is_safe(tmp_path):
    _write_profile(tmp_path, "process", "a", {"inherits": "b", "x": "1"})
    _write_profile(tmp_path, "process", "b", {"inherits": "a", "y": "2"})
    index = slice_tiles.build_profile_index(tmp_path)
    flat = slice_tiles.flatten_profile(index, "process", "a")
    assert flat["x"] == "1" and flat["y"] == "2"  # no infinite recursion


# --------------------------------------------------------------------------- #
# Full-config writing + infill override injection
# --------------------------------------------------------------------------- #

def test_write_full_configs_injects_infill_overrides(tmp_path):
    _write_profile(tmp_path, "machine", "m", {"nozzle_diameter": ["0.4"]})
    _write_profile(tmp_path, "process", "p", {
        "sparse_infill_density": "15%", "sparse_infill_pattern": "grid"})
    _write_profile(tmp_path, "filament", "f", {"filament_type": ["PLA"]})
    index = slice_tiles.build_profile_index(tmp_path)

    out = tmp_path / "out"
    out.mkdir()
    m_json, p_json, f_json = slice_tiles.write_full_configs(
        out, index, "m", "p", "f", density=7, pattern="honeycomb")

    process = json.loads(Path(p_json).read_text(encoding="utf-8"))
    assert process["sparse_infill_density"] == "7%"
    assert process["sparse_infill_pattern"] == "honeycomb"
    # Machine + filament configs written too.
    assert json.loads(Path(m_json).read_text())["nozzle_diameter"] == ["0.4"]
    assert json.loads(Path(f_json).read_text())["filament_type"] == ["PLA"]


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #

def test_list_printers_groups_by_model_and_nozzle(tmp_path):
    _write_profile(tmp_path, "machine", "P1S 0.4 nozzle", {
        "instantiation": "true", "printer_model": "Bambu Lab P1S",
        "nozzle_diameter": ["0.4"]})
    _write_profile(tmp_path, "machine", "P1S 0.6 nozzle", {
        "instantiation": "true", "printer_model": "Bambu Lab P1S",
        "nozzle_diameter": ["0.6"]})
    _write_profile(tmp_path, "machine", "common", {
        "instantiation": "false", "bed": "x"})  # not user-selectable
    index = slice_tiles.build_profile_index(tmp_path)

    printers = slice_tiles.list_printers(index)
    assert printers == {
        "Bambu Lab P1S": {
            "0.4": "P1S 0.4 nozzle",
            "0.6": "P1S 0.6 nozzle",
        }
    }


def test_list_processes_filters_compatible_and_defaults_first(tmp_path):
    _write_profile(tmp_path, "machine", "P1S 0.4 nozzle", {
        "instantiation": "true", "printer_model": "Bambu Lab P1S",
        "nozzle_diameter": ["0.4"],
        "default_print_profile": "0.20mm Standard"})
    _write_profile(tmp_path, "process", "0.20mm Standard", {
        "instantiation": "true",
        "compatible_printers": ["P1S 0.4 nozzle"]})
    _write_profile(tmp_path, "process", "0.08mm Fine", {
        "instantiation": "true",
        "compatible_printers": ["P1S 0.4 nozzle"]})
    _write_profile(tmp_path, "process", "Other printer only", {
        "instantiation": "true",
        "compatible_printers": ["X1C 0.4 nozzle"]})
    index = slice_tiles.build_profile_index(tmp_path)

    procs = slice_tiles.list_processes(index, "P1S 0.4 nozzle")
    assert "Other printer only" not in procs
    assert procs[0] == "0.20mm Standard"  # default moved to front
    assert "0.08mm Fine" in procs


# --------------------------------------------------------------------------- #
# G-code extraction
# --------------------------------------------------------------------------- #

def test_extract_plate_gcode(tmp_path):
    threemf = tmp_path / "tile.gcode.3mf"
    with zipfile.ZipFile(threemf, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", "; sparse_infill_density = 7%\nG1\n")
        zf.writestr("3D/3dmodel.model", "<model/>")
    dest = tmp_path / "tile.gcode"
    slice_tiles.extract_plate_gcode(threemf, dest)
    assert "sparse_infill_density = 7%" in dest.read_text(encoding="utf-8")


def test_extract_plate_gcode_falls_back_to_first_plate(tmp_path):
    threemf = tmp_path / "tile.gcode.3mf"
    with zipfile.ZipFile(threemf, "w") as zf:
        zf.writestr("Metadata/plate_2.gcode", "G1 X1\n")
    dest = tmp_path / "tile.gcode"
    slice_tiles.extract_plate_gcode(threemf, dest)
    assert dest.read_text(encoding="utf-8") == "G1 X1\n"


def test_extract_plate_gcode_raises_when_no_plate(tmp_path):
    threemf = tmp_path / "empty.gcode.3mf"
    with zipfile.ZipFile(threemf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(KeyError):
        slice_tiles.extract_plate_gcode(threemf, tmp_path / "x.gcode")


# --------------------------------------------------------------------------- #
# JSON settings: loading, normalising, resolving
# --------------------------------------------------------------------------- #

def test_load_settings_missing_file_returns_defaults(tmp_path):
    settings = slice_tiles.load_settings(tmp_path / "nope.json")
    assert settings == slice_tiles.DEFAULT_SETTINGS
    # A copy, not the module-level dict.
    assert settings is not slice_tiles.DEFAULT_SETTINGS


def test_load_settings_merges_over_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"sparse_infill_density": 8,
                                "printer": "Bambu Lab P1S"}), encoding="utf-8")
    settings = slice_tiles.load_settings(path)
    assert settings["sparse_infill_density"] == 8
    assert settings["printer"] == "Bambu Lab P1S"
    # Untouched keys keep their defaults.
    assert settings["sparse_infill_pattern"] == slice_tiles.DEFAULT_PATTERN


def test_load_settings_rejects_unknown_key(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"bogus": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        slice_tiles.load_settings(path)


def test_load_settings_ignores_possible_values_doc_block(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "sparse_infill_density": 9,
        "possible_values": {"anything": ["at", "all"]},  # doc-only, ignored
    }), encoding="utf-8")
    settings = slice_tiles.load_settings(path)
    assert settings["sparse_infill_density"] == 9
    assert "possible_values" not in settings  # never becomes a real setting


def test_normalize_pattern_accepts_value_and_label():
    assert slice_tiles.normalize_pattern("honeycomb") == "honeycomb"
    assert slice_tiles.normalize_pattern("Honeycomb") == "honeycomb"
    assert slice_tiles.normalize_pattern("3D Honeycomb") == "3dhoneycomb"
    assert slice_tiles.normalize_pattern("") == slice_tiles.DEFAULT_PATTERN


def test_normalize_pattern_rejects_unknown():
    with pytest.raises(ValueError):
        slice_tiles.normalize_pattern("not-a-pattern")


def test_clamp_density_bounds():
    assert slice_tiles.clamp_density(99) == slice_tiles.INFILL_DENSITY_MAX
    assert slice_tiles.clamp_density(0) == slice_tiles.INFILL_DENSITY_MIN
    assert slice_tiles.clamp_density(12) == 12


def _seed_install(tmp_path):
    """A minimal in-tree profile set so resolve_selection has data to chew on."""
    _write_profile(tmp_path, "machine", "P1S 0.4 nozzle", {
        "instantiation": "true", "printer_model": "Bambu Lab P1S",
        "nozzle_diameter": ["0.4"],
        "default_print_profile": "0.20mm Standard",
        "default_filament_profile": "Bambu PLA Basic"})
    _write_profile(tmp_path, "machine", "P1S 0.6 nozzle", {
        "instantiation": "true", "printer_model": "Bambu Lab P1S",
        "nozzle_diameter": ["0.6"]})
    _write_profile(tmp_path, "process", "0.20mm Standard", {
        "instantiation": "true", "compatible_printers": ["P1S 0.4 nozzle"]})
    _write_profile(tmp_path, "filament", "Bambu PLA Basic", {
        "instantiation": "true", "compatible_printers": ["P1S 0.4 nozzle"]})
    return slice_tiles.build_profile_index(tmp_path)


def test_resolve_selection_auto_fallbacks(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(slice_tiles.DEFAULT_SETTINGS)
    machine, process, filament, density, pattern = \
        slice_tiles.resolve_selection(index, settings)
    assert machine == "P1S 0.4 nozzle"      # first printer, 0.4 nozzle preferred
    assert process == "0.20mm Standard"      # machine default
    assert filament == "Bambu PLA Basic"     # machine default
    assert density == slice_tiles.INFILL_DENSITY_DEFAULT
    assert pattern == slice_tiles.DEFAULT_PATTERN


def test_resolve_selection_explicit_nozzle(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(slice_tiles.DEFAULT_SETTINGS,
                    printer="Bambu Lab P1S", nozzle="0.6")
    machine, *_ = slice_tiles.resolve_selection(index, settings)
    assert machine == "P1S 0.6 nozzle"


def test_resolve_selection_unknown_printer_raises(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(slice_tiles.DEFAULT_SETTINGS, printer="No Such Printer")
    with pytest.raises(ValueError):
        slice_tiles.resolve_selection(index, settings)


def test_resolve_selection_unknown_nozzle_raises(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(slice_tiles.DEFAULT_SETTINGS,
                    printer="Bambu Lab P1S", nozzle="0.8")
    with pytest.raises(ValueError):
        slice_tiles.resolve_selection(index, settings)


# --------------------------------------------------------------------------- #
# possible_values documentation block
# --------------------------------------------------------------------------- #

def test_match_printer_exact_substring_and_fallback():
    printers = {"Bambu Lab P2S": {}, "Bambu Lab A1": {}}
    assert slice_tiles._match_printer(printers, "Bambu Lab A1") == "Bambu Lab A1"
    assert slice_tiles._match_printer(printers, "P2S") == "Bambu Lab P2S"
    # No match / empty -> first model alphabetically.
    assert slice_tiles._match_printer(printers, "nope") == "Bambu Lab A1"
    assert slice_tiles._match_printer(printers, "") == "Bambu Lab A1"
    assert slice_tiles._match_printer({}, "x") is None


def test_build_possible_values_lists_every_setting(tmp_path):
    index = _seed_install(tmp_path)
    pv = slice_tiles.build_possible_values(index, printer="P1S", nozzle="0.4")
    # Density is a range, pattern is the full internal-value list.
    assert pv["sparse_infill_density"] == {
        "min": slice_tiles.INFILL_DENSITY_MIN,
        "max": slice_tiles.INFILL_DENSITY_MAX,
        "unit": "percent",
    }
    assert pv["sparse_infill_pattern"] == [v for v, _ in slice_tiles.INFILL_PATTERNS]
    assert pv["printer"] == ["Bambu Lab P1S"]
    assert pv["nozzle"] == ["0.4", "0.6"]  # both nozzles seeded for P1S
    # filament/quality enumerated for the (leniently matched) P1S machine.
    assert pv["filament"] == ["Bambu PLA Basic"]
    assert pv["quality"] == ["0.20mm Standard"]
    assert "P1S" in pv["_note"] or "Bambu Lab P1S" in pv["_note"]


def test_write_settings_file_roundtrips_and_strips_on_reload(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(slice_tiles.DEFAULT_SETTINGS,
                    sparse_infill_density=7, printer="Bambu Lab P1S")
    pv = slice_tiles.build_possible_values(index, "Bambu Lab P1S", "0.4")
    path = tmp_path / "out.json"
    slice_tiles.write_settings_file(path, settings, pv)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sparse_infill_density"] == 7
    assert "possible_values" in raw            # doc block written
    # Real settings come back; the doc block is stripped, not treated as a key.
    reloaded = slice_tiles.load_settings(path)
    assert reloaded["sparse_infill_density"] == 7
    assert "possible_values" not in reloaded


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #

def test_build_slice_command_shape():
    cmd = slice_tiles.build_slice_command(
        "bambu-studio", "tile.stl", "m.json", "p.json", "f.json",
        "/out", "tile.4.gcode.3mf")
    assert cmd[0] == "bambu-studio"
    assert "--load-settings" in cmd
    assert "m.json;p.json" in cmd
    assert cmd[cmd.index("--load-filaments") + 1] == "f.json"
    assert cmd[cmd.index("--export-3mf") + 1] == "tile.4.gcode.3mf"
    assert cmd[-1] == "tile.stl"
