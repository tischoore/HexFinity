"""Tests for the settings-JSON/CLI glue in ``scripts/slice_tiles.py``.

The reusable slicing mechanics (profile flattening, command building, G-code
extraction, settings resolution) now live in ``hexfinity/bambu_slicer.py`` and
are tested in ``tests/test_bambu_slicer.py``. This file covers only what's
still local to the standalone script: settings JSON load/write/refresh, the
``possible_values`` documentation block, and that ``run()``/``refresh_settings()``
wire correctly into ``bambu_slicer``. No tkinter, no real Bambu Studio call —
runs under any CPython (including Blender's bundled interpreter):

    python -m pytest scripts/tests -v
"""

import importlib.util
import json
from pathlib import Path

import pytest

# Import the standalone script by path (it lives in scripts/, not on sys.path).
_SCRIPT = Path(__file__).resolve().parents[1] / "slice_tiles.py"
_spec = importlib.util.spec_from_file_location("slice_tiles", _SCRIPT)
slice_tiles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slice_tiles)

bambu_slicer = slice_tiles.bambu_slicer


def _write_profile(root, category, name, data):
    data = dict(data)
    data.setdefault("name", name)
    path = Path(root) / category / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _seed_install(tmp_path):
    """A minimal in-tree profile set for build_possible_values/run() to chew on."""
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


# --------------------------------------------------------------------------- #
# JSON settings: loading, normalising
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
# run() / refresh_settings() wiring into bambu_slicer
# --------------------------------------------------------------------------- #

def test_run_wires_settings_into_bambu_slicer_slice_folder(tmp_path, monkeypatch):
    index = _seed_install(tmp_path)
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    monkeypatch.setattr(bambu_slicer, "find_bambu_executable",
                        lambda: "bambu-studio")
    monkeypatch.setattr(bambu_slicer, "profiles_dir", lambda exe: tmp_path)
    monkeypatch.setattr(bambu_slicer, "build_profile_index", lambda root: index)

    captured = {}

    def fake_slice_folder(exe, idx, folder, machine_name, process_name,
                          filament_name, density, pattern, log=print,
                          results=None):
        captured.update(machine_name=machine_name, process_name=process_name,
                        filament_name=filament_name, density=density,
                        pattern=pattern, folder=str(folder))
        return (0, 0)

    monkeypatch.setattr(bambu_slicer, "slice_folder", fake_slice_folder)

    settings = dict(slice_tiles.DEFAULT_SETTINGS, export_folder=str(export_dir),
                    printer="Bambu Lab P1S", sparse_infill_density=7)
    code = slice_tiles.run(settings, log=lambda msg: None)

    assert code == 0
    assert captured["machine_name"] == "P1S 0.4 nozzle"
    assert captured["process_name"] == "0.20mm Standard"
    assert captured["filament_name"] == "Bambu PLA Basic"
    assert captured["density"] == 7
    assert captured["folder"] == str(export_dir)


def test_run_reports_error_for_invalid_export_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(bambu_slicer, "find_bambu_executable",
                        lambda: "bambu-studio")
    monkeypatch.setattr(bambu_slicer, "profiles_dir", lambda exe: tmp_path)
    settings = dict(slice_tiles.DEFAULT_SETTINGS,
                    export_folder=str(tmp_path / "does_not_exist"))
    messages = []
    code = slice_tiles.run(settings, log=messages.append)
    assert code == 2
    assert any("not a valid directory" in m for m in messages)


def test_run_reports_error_when_bambu_studio_not_found(monkeypatch):
    monkeypatch.setattr(bambu_slicer, "find_bambu_executable", lambda: None)
    messages = []
    code = slice_tiles.run(dict(slice_tiles.DEFAULT_SETTINGS), log=messages.append)
    assert code == 2
    assert any("not found" in m for m in messages)
