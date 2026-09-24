#!/usr/bin/env python3
"""Batch-slice a HexFinity STL export folder to G-code with Bambu Studio.

Standalone CPython script (no ``bpy``) — run it directly:

    python scripts/slice_tiles.py [export_folder] [--settings path.json]

All slicing parameters (export folder, infill density + pattern, printer,
nozzle, filament, quality) are read from a JSON settings file —
``slice_tiles_settings.json`` next to this script by default. It then slices
every ``*.stl`` in the export folder with the locally installed Bambu Studio
command line. There is no interactive UI: edit the JSON, then run the script.

Why this exists / the three CLI realities it works around
---------------------------------------------------------
1. The Bambu Studio CLI only writes ``.gcode.3mf`` (via ``--export-3mf``); there
   is no direct ``.gcode`` output. Plain G-code lives inside that zip as
   ``Metadata/plate_1.gcode`` — we extract it ourselves (we keep *both* files).
2. The CLI has no generic ``--key=value`` override. Printer, nozzle, filament,
   infill density/pattern are all set by passing *full-config* JSON files to
   ``--load-settings "machine.json;process.json"`` and ``--load-filaments``.
3. The profiles shipped under ``resources/profiles/BBL`` are *partial* — they use
   ``"inherits"`` (and machine uses ``"include"``). The CLI needs full configs,
   so we flatten the inheritance chain ourselves and inject the infill overrides.

Print quantity
--------------
HexFinity deduplicates byte-identical tiles to a single STL, so one STL may need
to be printed several times. The count is derived from the export's
``manifest.json`` (number of rows referencing each ``file``). It is written into
the output name immediately before the extension, e.g. ``hex_q00_r00.4.gcode``
means "print four of these".

The reusable slicing mechanics (profile flattening, command building, G-code
extraction, settings resolution) live in ``hexfinity/bambu_slicer.py`` — a
bpy-free module also used by the in-Blender "Export + Slice" button, so
slicing *behavior* changes belong there, not here. This file keeps only the
settings-JSON/CLI-specific glue and is covered (together with
``bambu_slicer.py``, via ``tests/test_bambu_slicer.py``) by
``scripts/tests/test_slice_tiles.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hexfinity"))
import bambu_slicer  # noqa: E402  (must follow the sys.path insertion above)

INFILL_DENSITY_MIN = bambu_slicer.INFILL_DENSITY_MIN
INFILL_DENSITY_MAX = bambu_slicer.INFILL_DENSITY_MAX
INFILL_DENSITY_DEFAULT = bambu_slicer.INFILL_DENSITY_DEFAULT
INFILL_PATTERNS = bambu_slicer.INFILL_PATTERNS
DEFAULT_PATTERN = bambu_slicer.DEFAULT_PATTERN

#: Settings file read by default — lives next to this script.
SETTINGS_FILENAME = "slice_tiles_settings.json"

#: Every parameter the old tkinter UI exposed, now as JSON keys + defaults.
#: ``printer``/``nozzle``/``filament``/``quality`` may be left as ``""`` to fall
#: back to auto-detection (first printer, 0.4 nozzle, machine's default presets).
DEFAULT_SETTINGS = {
    "export_folder": "",
    "sparse_infill_density": INFILL_DENSITY_DEFAULT,
    "sparse_infill_pattern": DEFAULT_PATTERN,
    "printer": "",
    "nozzle": "",
    "filament": "",
    "quality": "",
}

#: Documentation-only key(s): present in the settings file for reference but
#: ignored at slice time (stripped in :func:`load_settings`). ``possible_values``
#: lists/ranges every valid value for each setting; regenerate it with
#: ``--refresh`` so it tracks the installed Bambu Studio.
DOC_KEY = "possible_values"


# --------------------------------------------------------------------------- #
# JSON settings: loading, normalising, resolving against the install
# --------------------------------------------------------------------------- #

def default_settings_path():
    """Path to ``slice_tiles_settings.json`` next to this script."""
    return Path(__file__).resolve().parent / SETTINGS_FILENAME


def load_settings(path=None):
    """Load a settings JSON, merged over :data:`DEFAULT_SETTINGS`.

    Missing keys take their default; a missing file yields the defaults
    unchanged. ``path`` defaults to :func:`default_settings_path`.
    """
    path = Path(path) if path else default_settings_path()
    settings = dict(DEFAULT_SETTINGS)
    if path.is_file():
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if not isinstance(user, dict):
            raise ValueError(f"{path}: settings must be a JSON object")
        user = {k: v for k, v in user.items() if k != DOC_KEY}  # doc-only, ignore
        unknown = set(user) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError(
                f"{path}: unknown setting(s) {sorted(unknown)}; "
                f"valid keys are {sorted(DEFAULT_SETTINGS)}"
            )
        settings.update(user)
    return settings


# --------------------------------------------------------------------------- #
# Documentation: the ``possible_values`` reference section
# --------------------------------------------------------------------------- #

def _match_printer(printers, wanted):
    """Resolve a (possibly shorthand) printer name to an installed model.

    Returns an exact key if present, else the first model whose name *contains*
    ``wanted`` (case-insensitive, so ``"P2S"`` → ``"Bambu Lab P2S"``), else the
    first model. Used only to choose which printer the filament/quality
    reference lists are enumerated for; it never affects slicing.
    """
    models = sorted(printers)
    if not models:
        return None
    if wanted in printers:
        return wanted
    wl = str(wanted or "").strip().lower()
    if wl:
        for model in models:
            if wl in model.lower():
                return model
    return models[0]


def build_possible_values(index, printer=None, nozzle=None):
    """Build the documentation-only ``possible_values`` block.

    For each setting it lists every valid value (or the numeric range). The
    printer/nozzle lists come straight from the install; ``filament`` and
    ``quality`` depend on the chosen printer+nozzle, so they are enumerated for
    the printer named in the settings (resolved leniently — see
    :func:`_match_printer`), defaulting to 0.4mm. Purely informational: the
    slicer ignores this block (see :data:`DOC_KEY`).
    """
    printers = bambu_slicer.list_printers(index)
    model = _match_printer(printers, printer)
    nozzles = printers.get(model, {}) if model else {}
    noz = str(nozzle or "").strip()
    if noz not in nozzles:
        noz = "0.4" if "0.4" in nozzles else (sorted(nozzles)[0] if nozzles else "")
    machine_name = nozzles.get(noz)

    filaments = bambu_slicer.list_filaments(index, machine_name) if machine_name else []
    qualities = bambu_slicer.list_processes(index, machine_name) if machine_name else []

    note = (
        "Reference only — this block is ignored when slicing. "
        "printer/nozzle/filament/quality are enumerated from the installed "
        "Bambu Studio; 'filament' and 'quality' depend on the selected "
        f"printer+nozzle (listed here for {model} / {noz}mm). "
        "Regenerate with: python scripts/slice_tiles.py --refresh"
    )
    return {
        "_note": note,
        "export_folder": "Any existing directory holding the exported *.stl "
                         "files and manifest.json.",
        "sparse_infill_density": {
            "min": INFILL_DENSITY_MIN,
            "max": INFILL_DENSITY_MAX,
            "unit": "percent",
        },
        "sparse_infill_pattern": [value for value, _label in INFILL_PATTERNS],
        "printer": sorted(printers),
        "nozzle": sorted(nozzles),
        "filament": filaments,
        "quality": qualities,
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def write_settings_file(path, settings, possible_values):
    """Write ``settings`` (the real keys) plus a ``possible_values`` block.

    Real settings come first, in :data:`DEFAULT_SETTINGS` order, so the file
    stays diff-friendly; the documentation block is appended last.
    """
    out = {key: settings.get(key, DEFAULT_SETTINGS[key]) for key in DEFAULT_SETTINGS}
    out[DOC_KEY] = possible_values
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def refresh_settings(settings, settings_path, log=print):
    """Regenerate the ``possible_values`` block in the settings file.

    Keeps the real settings untouched; recomputes the reference lists from the
    installed Bambu Studio (using the file's ``printer``/``nozzle`` to pick which
    printer to enumerate filament/quality for). Returns an exit code.
    """
    exe = bambu_slicer.find_bambu_executable()
    root_profiles = bambu_slicer.profiles_dir(exe) if exe else None
    if not root_profiles:
        log("ERROR: cannot refresh — Bambu Studio install or its bundled "
            "profiles (resources/profiles/BBL) were not found.")
        return 2
    index = bambu_slicer.build_profile_index(root_profiles)
    pv = build_possible_values(index, settings.get("printer"),
                               settings.get("nozzle"))
    write_settings_file(settings_path, settings, pv)
    log(f"Refreshed possible_values in {settings_path}")
    return 0


def run(settings, settings_path=None, log=print):
    """Resolve ``settings`` against the install and slice. Returns an exit code.

    ``log`` receives progress lines (defaults to ``print``). ``settings_path``
    is only used for nicer diagnostics.
    """
    exe = bambu_slicer.find_bambu_executable()
    if not exe:
        log("ERROR: Bambu Studio CLI not found on PATH or at "
            f"{bambu_slicer._DEFAULT_WIN_EXE}.")
        return 2
    root_profiles = bambu_slicer.profiles_dir(exe)
    if not root_profiles:
        log("ERROR: Bambu Studio found but its bundled profiles "
            "(resources/profiles/BBL) are missing.")
        return 2

    folder = str(settings.get("export_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        where = f" (from {settings_path})" if settings_path else ""
        log(f"ERROR: export_folder {folder!r} is not a valid directory{where}.")
        return 2

    index = bambu_slicer.build_profile_index(root_profiles)
    try:
        machine_name, process_name, filament_name, density, pattern = \
            bambu_slicer.resolve_selection(index, settings)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 2

    log(f"Bambu Studio: {exe}")
    ok, fail = bambu_slicer.slice_folder(
        exe, index, folder, machine_name, process_name, filament_name,
        density, pattern, log=log,
    )
    return 0 if fail == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "folder", nargs="?", default=None,
        help="HexFinity export folder; overrides export_folder in the settings.",
    )
    parser.add_argument(
        "--settings", default=None,
        help=f"Settings JSON (default: {SETTINGS_FILENAME} next to this script).",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Rewrite the settings file's 'possible_values' reference block "
             "from the installed Bambu Studio, then exit (does not slice).",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.settings)
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not load settings: {exc}")
        return 2
    if args.folder:
        settings["export_folder"] = args.folder

    settings_path = args.settings or str(default_settings_path())
    if args.refresh:
        return refresh_settings(settings, settings_path)
    return run(settings, settings_path=settings_path)


if __name__ == "__main__":
    sys.exit(main())
