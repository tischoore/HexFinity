"""Tests for the bpy-free Bambu Studio slicing mechanics in
``hexfinity/bambu_slicer.py`` — no tkinter, no real Bambu Studio call, so
these run under any CPython (including Blender's bundled interpreter):

    python -m pytest tests -v

(``tests/conftest.py`` puts ``hexfinity/`` on ``sys.path`` so this module can
be imported directly, the same way it's imported at runtime by
``hexfinity/operators.py`` and by ``scripts/slice_tiles.py``.)
"""

import json
import zipfile
from pathlib import Path

import pytest

import bambu_slicer


# --------------------------------------------------------------------------- #
# tile_counts / count_for / read_manifest
# --------------------------------------------------------------------------- #

def test_tile_counts_groups_by_file():
    rows = [
        {"q": 0, "r": 0, "file": "hex_q00_r00.stl", "custom": False},
        {"q": 1, "r": 0, "file": "hex_q00_r00.stl", "custom": False},
        {"q": 2, "r": 0, "file": "hex_q00_r00.stl", "custom": False},
        {"q": 0, "r": 1, "file": "hex_q00_r01_abcd1234.stl", "custom": True},
    ]
    counts = bambu_slicer.tile_counts(rows)
    assert counts == {"hex_q00_r00.stl": 3, "hex_q00_r01_abcd1234.stl": 1}


def test_count_for_defaults_to_one_when_absent():
    counts = {"hex_q00_r00.stl": 4}
    assert bambu_slicer.count_for(counts, "hex_q00_r00.stl") == 4
    assert bambu_slicer.count_for(counts, "not_in_manifest.stl") == 1


def test_tile_counts_ignores_rows_without_file():
    assert bambu_slicer.tile_counts([{"q": 0, "r": 0}]) == {}


def test_read_manifest_missing_returns_empty(tmp_path):
    assert bambu_slicer.read_manifest(tmp_path) == []


def test_read_manifest_roundtrip(tmp_path):
    rows = [{"q": 0, "r": 0, "file": "a.stl", "custom": False}]
    (tmp_path / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    assert bambu_slicer.read_manifest(tmp_path) == rows


# --------------------------------------------------------------------------- #
# Output naming
# --------------------------------------------------------------------------- #

def test_gcode_name_puts_count_before_extension():
    assert bambu_slicer.gcode_name("hex_q00_r00", 4) == "hex_q00_r00.4.gcode"


def test_threemf_name():
    assert bambu_slicer.threemf_name("hex_q00_r00", 4) == "hex_q00_r00.4.gcode.3mf"


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
    index = bambu_slicer.build_profile_index(tmp_path)
    flat = bambu_slicer.flatten_profile(index, "process", "child")

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
    index = bambu_slicer.build_profile_index(tmp_path)
    flat = bambu_slicer.flatten_profile(index, "machine", "p1s")

    assert flat["bed_size"] == "256"                 # from inherits
    assert flat["machine_start_gcode"] == "G28"      # from include
    assert flat["nozzle_diameter"] == ["0.4"]        # own
    assert "include" not in flat


def test_flatten_cycle_is_safe(tmp_path):
    _write_profile(tmp_path, "process", "a", {"inherits": "b", "x": "1"})
    _write_profile(tmp_path, "process", "b", {"inherits": "a", "y": "2"})
    index = bambu_slicer.build_profile_index(tmp_path)
    flat = bambu_slicer.flatten_profile(index, "process", "a")
    assert flat["x"] == "1" and flat["y"] == "2"  # no infinite recursion


# --------------------------------------------------------------------------- #
# Full-config writing + infill override injection
# --------------------------------------------------------------------------- #

def test_write_full_configs_injects_infill_overrides(tmp_path):
    _write_profile(tmp_path, "machine", "m", {"nozzle_diameter": ["0.4"]})
    _write_profile(tmp_path, "process", "p", {
        "sparse_infill_density": "15%", "sparse_infill_pattern": "grid"})
    _write_profile(tmp_path, "filament", "f", {"filament_type": ["PLA"]})
    index = bambu_slicer.build_profile_index(tmp_path)

    out = tmp_path / "out"
    out.mkdir()
    m_json, p_json, f_json = bambu_slicer.write_full_configs(
        out, index, "m", "p", "f", density=7, pattern="honeycomb",
        adaptive_layer_height=True)

    process = json.loads(Path(p_json).read_text(encoding="utf-8"))
    assert process["sparse_infill_density"] == "7%"
    assert process["sparse_infill_pattern"] == "honeycomb"
    assert process["adaptive_layer_height"] == "1"
    # Machine + filament configs written too.
    assert json.loads(Path(m_json).read_text())["nozzle_diameter"] == ["0.4"]
    assert json.loads(Path(f_json).read_text())["filament_type"] == ["PLA"]


def test_write_full_configs_adaptive_layer_height_off(tmp_path):
    _write_profile(tmp_path, "machine", "m", {"nozzle_diameter": ["0.4"]})
    _write_profile(tmp_path, "process", "p", {
        "sparse_infill_density": "15%", "sparse_infill_pattern": "grid"})
    _write_profile(tmp_path, "filament", "f", {"filament_type": ["PLA"]})
    index = bambu_slicer.build_profile_index(tmp_path)

    out = tmp_path / "out"
    out.mkdir()
    _m_json, p_json, _f_json = bambu_slicer.write_full_configs(
        out, index, "m", "p", "f", density=7, pattern="honeycomb",
        adaptive_layer_height=False)

    process = json.loads(Path(p_json).read_text(encoding="utf-8"))
    assert process["adaptive_layer_height"] == "0"


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
    index = bambu_slicer.build_profile_index(tmp_path)

    printers = bambu_slicer.list_printers(index)
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
    index = bambu_slicer.build_profile_index(tmp_path)

    procs = bambu_slicer.list_processes(index, "P1S 0.4 nozzle")
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
    bambu_slicer.extract_plate_gcode(threemf, dest)
    assert "sparse_infill_density = 7%" in dest.read_text(encoding="utf-8")


def test_extract_plate_gcode_falls_back_to_first_plate(tmp_path):
    threemf = tmp_path / "tile.gcode.3mf"
    with zipfile.ZipFile(threemf, "w") as zf:
        zf.writestr("Metadata/plate_2.gcode", "G1 X1\n")
    dest = tmp_path / "tile.gcode"
    bambu_slicer.extract_plate_gcode(threemf, dest)
    assert dest.read_text(encoding="utf-8") == "G1 X1\n"


def test_extract_plate_gcode_raises_when_no_plate(tmp_path):
    threemf = tmp_path / "empty.gcode.3mf"
    with zipfile.ZipFile(threemf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(KeyError):
        bambu_slicer.extract_plate_gcode(threemf, tmp_path / "x.gcode")


# --------------------------------------------------------------------------- #
# Settings normalisation / resolution
# --------------------------------------------------------------------------- #

def test_normalize_pattern_accepts_value_and_label():
    assert bambu_slicer.normalize_pattern("honeycomb") == "honeycomb"
    assert bambu_slicer.normalize_pattern("Honeycomb") == "honeycomb"
    assert bambu_slicer.normalize_pattern("3D Honeycomb") == "3dhoneycomb"
    assert bambu_slicer.normalize_pattern("") == bambu_slicer.DEFAULT_PATTERN


def test_normalize_pattern_rejects_unknown():
    with pytest.raises(ValueError):
        bambu_slicer.normalize_pattern("not-a-pattern")


def test_clamp_density_bounds():
    assert bambu_slicer.clamp_density(99) == bambu_slicer.INFILL_DENSITY_MAX
    assert bambu_slicer.clamp_density(0) == bambu_slicer.INFILL_DENSITY_MIN
    assert bambu_slicer.clamp_density(12) == 12


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
    return bambu_slicer.build_profile_index(tmp_path)


_DEFAULT_SETTINGS = {
    "sparse_infill_density": bambu_slicer.INFILL_DENSITY_DEFAULT,
    "sparse_infill_pattern": bambu_slicer.DEFAULT_PATTERN,
    "adaptive_layer_height": False,
    "printer": "",
    "nozzle": "",
    "filament": "",
    "quality": "",
}


def test_resolve_selection_auto_fallbacks(tmp_path):
    index = _seed_install(tmp_path)
    machine, process, filament, density, pattern, adaptive_layer_height = \
        bambu_slicer.resolve_selection(index, dict(_DEFAULT_SETTINGS))
    assert machine == "P1S 0.4 nozzle"      # first printer, 0.4 nozzle preferred
    assert process == "0.20mm Standard"      # machine default
    assert filament == "Bambu PLA Basic"     # machine default
    assert density == bambu_slicer.INFILL_DENSITY_DEFAULT
    assert pattern == bambu_slicer.DEFAULT_PATTERN
    assert adaptive_layer_height is False


def test_resolve_selection_adaptive_layer_height_passthrough(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(_DEFAULT_SETTINGS, adaptive_layer_height=True)
    *_rest, adaptive_layer_height = bambu_slicer.resolve_selection(index, settings)
    assert adaptive_layer_height is True


def test_resolve_selection_explicit_nozzle(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(_DEFAULT_SETTINGS, printer="Bambu Lab P1S", nozzle="0.6")
    machine, *_ = bambu_slicer.resolve_selection(index, settings)
    assert machine == "P1S 0.6 nozzle"


def test_resolve_selection_unknown_printer_raises(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(_DEFAULT_SETTINGS, printer="No Such Printer")
    with pytest.raises(ValueError):
        bambu_slicer.resolve_selection(index, settings)


def test_resolve_selection_unknown_nozzle_raises(tmp_path):
    index = _seed_install(tmp_path)
    settings = dict(_DEFAULT_SETTINGS, printer="Bambu Lab P1S", nozzle="0.8")
    with pytest.raises(ValueError):
        bambu_slicer.resolve_selection(index, settings)


# --------------------------------------------------------------------------- #
# resolve_machine: lenient (never-raising) dropdown-dependency resolution
# --------------------------------------------------------------------------- #

def test_resolve_machine_auto_fallbacks(tmp_path):
    index = _seed_install(tmp_path)
    model, nozzle, machine = bambu_slicer.resolve_machine(index, "", "")
    assert (model, nozzle, machine) == (
        "Bambu Lab P1S", "0.4", "P1S 0.4 nozzle")


def test_resolve_machine_explicit_nozzle(tmp_path):
    index = _seed_install(tmp_path)
    model, nozzle, machine = bambu_slicer.resolve_machine(
        index, "Bambu Lab P1S", "0.6")
    assert (model, nozzle, machine) == (
        "Bambu Lab P1S", "0.6", "P1S 0.6 nozzle")


def test_resolve_machine_unrecognized_values_fall_back_instead_of_raising(tmp_path):
    index = _seed_install(tmp_path)
    model, nozzle, machine = bambu_slicer.resolve_machine(
        index, "No Such Printer", "9.9")
    assert (model, nozzle, machine) == (
        "Bambu Lab P1S", "0.4", "P1S 0.4 nozzle")


def test_resolve_machine_no_printers_returns_none_triple(tmp_path):
    index = bambu_slicer.build_profile_index(tmp_path)  # empty install
    assert bambu_slicer.resolve_machine(index, "", "") == (None, None, None)


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #

def test_build_slice_command_shape():
    cmd = bambu_slicer.build_slice_command(
        "bambu-studio", "tile.stl", "m.json", "p.json", "f.json",
        "/out", "tile.4.gcode.3mf")
    assert cmd[0] == "bambu-studio"
    assert "--load-settings" in cmd
    assert "m.json;p.json" in cmd
    assert cmd[cmd.index("--load-filaments") + 1] == "f.json"
    assert cmd[cmd.index("--export-3mf") + 1] == "tile.4.gcode.3mf"
    assert cmd[-1] == "tile.stl"


# --------------------------------------------------------------------------- #
# slice_folder: results= per-tile outcome list
# --------------------------------------------------------------------------- #

def test_slice_folder_results_list_records_success_and_failure(tmp_path, monkeypatch):
    index = _seed_install(tmp_path)
    (tmp_path / "ok.stl").write_bytes(b"solid ok\nendsolid\n")
    (tmp_path / "bad.stl").write_bytes(b"solid bad\nendsolid\n")

    def fake_slice_one(exe, stl_path, machine_json, process_json,
                       filament_json, outdir, out_3mf_name):
        out_path = Path(outdir) / out_3mf_name
        if Path(stl_path).stem == "bad":
            raise RuntimeError("boom")
        with zipfile.ZipFile(out_path, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", "G1\n")
        return str(out_path)

    monkeypatch.setattr(bambu_slicer, "slice_one", fake_slice_one)

    results = []
    ok, fail = bambu_slicer.slice_folder(
        "bambu-studio", index, tmp_path,
        "P1S 0.4 nozzle", "0.20mm Standard", "Bambu PLA Basic",
        15, "honeycomb", False, log=lambda msg: None, results=results,
    )
    assert (ok, fail) == (1, 1)
    outcomes = {Path(path).stem: success for path, success, _msg in results}
    assert outcomes == {"ok": True, "bad": False}


# --------------------------------------------------------------------------- #
# Settings JSON I/O — shared by scripts/slice_tiles.py and the in-Blender
# Export + Slice dialog.
# --------------------------------------------------------------------------- #

def test_load_settings_missing_file_returns_defaults(tmp_path):
    settings = bambu_slicer.load_settings(tmp_path / "nope.json")
    assert settings == bambu_slicer.DEFAULT_SETTINGS
    assert settings is not bambu_slicer.DEFAULT_SETTINGS  # a copy


def test_load_settings_none_path_returns_defaults():
    settings = bambu_slicer.load_settings(None)
    assert settings == bambu_slicer.DEFAULT_SETTINGS


def test_load_settings_merges_over_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"sparse_infill_density": 8,
                                "printer": "Bambu Lab P1S"}), encoding="utf-8")
    settings = bambu_slicer.load_settings(path)
    assert settings["sparse_infill_density"] == 8
    assert settings["printer"] == "Bambu Lab P1S"
    assert settings["sparse_infill_pattern"] == bambu_slicer.DEFAULT_PATTERN


def test_load_settings_rejects_unknown_key(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"bogus": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        bambu_slicer.load_settings(path)


def test_load_settings_ignores_possible_values_doc_block(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "sparse_infill_density": 9,
        "possible_values": {"anything": ["at", "all"]},
    }), encoding="utf-8")
    settings = bambu_slicer.load_settings(path)
    assert settings["sparse_infill_density"] == 9
    assert "possible_values" not in settings


def test_write_settings_file_roundtrips_and_strips_on_reload(tmp_path):
    settings = dict(bambu_slicer.DEFAULT_SETTINGS,
                    sparse_infill_density=7, printer="Bambu Lab P1S")
    path = tmp_path / "out.json"
    bambu_slicer.write_settings_file(path, settings, {"_note": "ref"})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sparse_infill_density"] == 7
    assert raw["possible_values"] == {"_note": "ref"}
    reloaded = bambu_slicer.load_settings(path)
    assert reloaded["sparse_infill_density"] == 7
    assert "possible_values" not in reloaded


def test_save_settings_fields_creates_file_with_merged_defaults(tmp_path):
    path = tmp_path / "s.json"
    bambu_slicer.save_settings_fields(path, printer="Bambu Lab P1S")
    settings = bambu_slicer.load_settings(path)
    assert settings["printer"] == "Bambu Lab P1S"
    assert settings["nozzle"] == ""  # untouched fields keep their default


def test_save_settings_fields_merges_over_existing_values(tmp_path):
    path = tmp_path / "s.json"
    bambu_slicer.save_settings_fields(path, printer="Bambu Lab P1S", nozzle="0.4")
    bambu_slicer.save_settings_fields(path, nozzle="0.6")
    settings = bambu_slicer.load_settings(path)
    assert settings["printer"] == "Bambu Lab P1S"  # untouched by the 2nd call
    assert settings["nozzle"] == "0.6"


def test_save_settings_fields_preserves_possible_values_block(tmp_path):
    path = tmp_path / "s.json"
    bambu_slicer.write_settings_file(
        path, bambu_slicer.DEFAULT_SETTINGS, {"_note": "keep me"})
    bambu_slicer.save_settings_fields(path, printer="Bambu Lab P1S")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["possible_values"] == {"_note": "keep me"}
    assert raw["printer"] == "Bambu Lab P1S"


def test_save_settings_fields_roundtrips_adaptive_layer_height(tmp_path):
    path = tmp_path / "s.json"
    bambu_slicer.save_settings_fields(path, adaptive_layer_height=True)
    settings = bambu_slicer.load_settings(path)
    assert settings["adaptive_layer_height"] is True


def test_save_settings_fields_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "s.json"
    bambu_slicer.save_settings_fields(path, printer="Bambu Lab P1S")
    assert path.is_file()
    assert bambu_slicer.load_settings(path)["printer"] == "Bambu Lab P1S"


def test_blender_settings_path_found_when_scripts_dir_exists(tmp_path):
    (tmp_path / "hexfinity").mkdir()
    (tmp_path / "scripts").mkdir()
    result = bambu_slicer.blender_settings_path(base=tmp_path / "hexfinity")
    assert result == tmp_path / "scripts" / bambu_slicer.SETTINGS_FILENAME


def test_blender_settings_path_none_when_scripts_dir_missing(tmp_path):
    (tmp_path / "hexfinity").mkdir()
    result = bambu_slicer.blender_settings_path(base=tmp_path / "hexfinity")
    assert result is None


# --------------------------------------------------------------------------- #
# Enumeration cache
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clear_bambu_cache():
    """Every cache test starts clean and leaves no state for other tests."""
    bambu_slicer.clear_cache()
    yield
    bambu_slicer.clear_cache()


def _counting(monkeypatch, name):
    """Wrap bambu_slicer.<name> to count calls, returning the counter list."""
    calls = []
    original = getattr(bambu_slicer, name)

    def wrapper(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(bambu_slicer, name, wrapper)
    return calls


def test_cached_index_scans_once_until_clear_cache(tmp_path, monkeypatch):
    calls = _counting(monkeypatch, "build_profile_index")
    bambu_slicer.cached_index(tmp_path)
    bambu_slicer.cached_index(tmp_path)
    assert len(calls) == 1
    bambu_slicer.clear_cache()
    bambu_slicer.cached_index(tmp_path)
    assert len(calls) == 2


def test_cached_printers_scans_once_until_clear_cache(tmp_path, monkeypatch):
    _seed_install(tmp_path)
    calls = _counting(monkeypatch, "list_printers")
    bambu_slicer.cached_printers(tmp_path)
    bambu_slicer.cached_printers(tmp_path)
    assert len(calls) == 1
    bambu_slicer.clear_cache()
    bambu_slicer.cached_printers(tmp_path)
    assert len(calls) == 2


def test_cached_filaments_scans_once_per_machine_until_clear_cache(tmp_path, monkeypatch):
    _seed_install(tmp_path)
    calls = _counting(monkeypatch, "list_filaments")
    bambu_slicer.cached_filaments(tmp_path, "P1S 0.4 nozzle")
    bambu_slicer.cached_filaments(tmp_path, "P1S 0.4 nozzle")
    assert len(calls) == 1
    bambu_slicer.clear_cache()
    bambu_slicer.cached_filaments(tmp_path, "P1S 0.4 nozzle")
    assert len(calls) == 2


def test_cached_processes_scans_once_per_machine_until_clear_cache(tmp_path, monkeypatch):
    _seed_install(tmp_path)
    calls = _counting(monkeypatch, "list_processes")
    bambu_slicer.cached_processes(tmp_path, "P1S 0.4 nozzle")
    bambu_slicer.cached_processes(tmp_path, "P1S 0.4 nozzle")
    assert len(calls) == 1
    bambu_slicer.clear_cache()
    bambu_slicer.cached_processes(tmp_path, "P1S 0.4 nozzle")
    assert len(calls) == 2
