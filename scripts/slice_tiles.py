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

The pure helpers below (everything except the settings/IO glue and the
subprocess call) are import-safe and covered by
``scripts/tests/test_slice_tiles.py``.
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Default Bambu Studio install location on Windows (fallback when not on PATH).
_DEFAULT_WIN_EXE = r"C:\Program Files\Bambu Studio\bambu-studio.exe"

#: Executable names to probe on PATH, in order.
_EXE_NAMES = ("bambu-studio", "bambu_studio", "bambu-studio.exe", "bambustudio")

#: Bambu vendor folder inside ``resources/profiles`` we read presets from.
_VENDOR = "BBL"

#: Sparse-infill density UI bounds (percent), per the feature request.
INFILL_DENSITY_MIN = 5
INFILL_DENSITY_MAX = 20
INFILL_DENSITY_DEFAULT = 15

#: ``sparse_infill_pattern`` enum: (internal value, human label).
#: Taken verbatim and in order from Bambu Studio ``PrintConfig.cpp``
#: (``def = this->add("sparse_infill_pattern", coEnum)``). Honeycomb is the
#: HexFinity default (Bambu's own default is Cubic).
INFILL_PATTERNS = [
    ("concentric", "Concentric"),
    ("zig-zag", "Rectilinear"),
    ("grid", "Grid"),
    ("line", "Line"),
    ("cubic", "Cubic"),
    ("triangles", "Triangles"),
    ("tri-hexagon", "Tri-hexagon"),
    ("gyroid", "Gyroid"),
    ("honeycomb", "Honeycomb"),
    ("adaptivecubic", "Adaptive Cubic"),
    ("alignedrectilinear", "Aligned Rectilinear"),
    ("3dhoneycomb", "3D Honeycomb"),
    ("hilbertcurve", "Hilbert Curve"),
    ("archimedeanchords", "Archimedean Chords"),
    ("octagramspiral", "Octagram Spiral"),
    ("supportcubic", "Support Cubic"),
    ("lightning", "Lightning"),
    ("crosshatch", "Cross Hatch"),
    ("zigzag", "Zig Zag"),
    ("crosszag", "Cross Zag"),
    ("lockedzag", "Locked Zag"),
    ("2dlattice", "2D Lattice"),
]

DEFAULT_PATTERN = "honeycomb"

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
# Locating the install
# --------------------------------------------------------------------------- #

def find_bambu_executable():
    """Return a path to the Bambu Studio CLI, or ``None`` if not found.

    Prefers an executable on ``PATH`` (any of :data:`_EXE_NAMES`); falls back to
    the default Windows install path.
    """
    for name in _EXE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    if os.path.isfile(_DEFAULT_WIN_EXE):
        return _DEFAULT_WIN_EXE
    return None


def profiles_dir(exe_path):
    """``resources/profiles/BBL`` next to ``exe_path``, or ``None`` if absent.

    Works whether ``exe_path`` came from PATH (a real file) — we resolve it to
    the install directory and look for ``resources/profiles/<vendor>``.
    """
    if not exe_path:
        return None
    install = Path(exe_path).resolve().parent
    cand = install / "resources" / "profiles" / _VENDOR
    if cand.is_dir():
        return cand
    return None


# --------------------------------------------------------------------------- #
# Profile loading + inheritance flattening
# --------------------------------------------------------------------------- #

_CATEGORY_DIRS = ("machine", "process", "filament")


def build_profile_index(root):
    """Map ``{category: {profile_name: path}}`` for ``machine/process/filament``.

    ``root`` is a profiles dir (see :func:`profiles_dir`). A profile's key is its
    JSON ``"name"`` field (falling back to the file stem). Files that fail to
    parse are skipped silently — a single bad preset must not break enumeration.
    """
    root = Path(root)
    index = {cat: {} for cat in _CATEGORY_DIRS}
    for cat in _CATEGORY_DIRS:
        cat_dir = root / cat
        if not cat_dir.is_dir():
            continue
        for path in cat_dir.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            name = data.get("name") or path.stem
            index[cat][name] = path
    return index


def _load_raw(index, category, name):
    """Parse and return the raw dict for a named profile (``{}`` if missing)."""
    path = index.get(category, {}).get(name)
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _deep_merge(base, overlay):
    """Merge ``overlay`` into ``base`` in place; child values win.

    Nested dicts merge recursively; everything else (scalars, lists) is replaced
    wholesale — matching how Bambu resolves a child preset over its parent.
    """
    for key, value in overlay.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def flatten_profile(index, category, name, _seen=None):
    """Resolve a profile's ``inherits``/``include`` chain into one full config.

    Resolution order (lowest precedence first): the ``inherits`` parent chain,
    then each profile named in ``include`` (machine G-code templates), then the
    profile's own keys. ``inherits`` and ``include`` are stripped from the
    result. Cycles are guarded against via ``_seen``.
    """
    if _seen is None:
        _seen = set()
    if name in _seen:
        return {}
    _seen.add(name)

    data = _load_raw(index, category, name)
    result = {}

    parent = data.get("inherits")
    if parent:
        _deep_merge(result, flatten_profile(index, category, parent, _seen))

    for inc in data.get("include", []) or []:
        _deep_merge(result, flatten_profile(index, category, inc, _seen))

    own = {k: v for k, v in data.items() if k not in ("inherits", "include")}
    _deep_merge(result, own)
    return result


# --------------------------------------------------------------------------- #
# Enumeration for the UI dropdowns
# --------------------------------------------------------------------------- #

def _is_instantiable(data):
    """Whether a preset is user-selectable (``instantiation`` true)."""
    return str(data.get("instantiation", "")).lower() == "true"


def list_printers(index):
    """Return ``{printer_model: {nozzle: machine_profile_name}}``.

    Only instantiable machine presets that name a model and a nozzle diameter
    are included; Printer + Nozzle together pick exactly one machine preset.
    """
    printers = collections.defaultdict(dict)
    for name, path in index.get("machine", {}).items():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not _is_instantiable(data):
            continue
        model = data.get("printer_model")
        nozzles = data.get("nozzle_diameter")
        if not model or not nozzles:
            continue
        printers[model][str(nozzles[0])] = name
    return {model: dict(noz) for model, noz in printers.items()}


def _compatible_names(index, category, printer_name):
    """Instantiable presets in ``category`` whose ``compatible_printers`` lists
    ``printer_name`` (or that declare none), as a sorted list of names."""
    out = []
    for name, path in index.get(category, {}).items():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not _is_instantiable(data):
            continue
        compat = data.get("compatible_printers")
        if not compat or printer_name in compat:
            out.append(name)
    return sorted(out)


def _default_first(names, default):
    """Return ``names`` with ``default`` moved to the front if present."""
    names = list(names)
    if default and default in names:
        names.remove(default)
        names.insert(0, default)
    return names


def list_processes(index, machine_name):
    """Compatible process presets for a machine, default (its
    ``default_print_profile``) first."""
    machine = flatten_profile(index, "machine", machine_name)
    default = machine.get("default_print_profile")
    return _default_first(_compatible_names(index, "process", machine_name), default)


def list_filaments(index, machine_name):
    """Compatible filament presets for a machine, default (its
    ``default_filament_profile``) first."""
    machine = flatten_profile(index, "machine", machine_name)
    default = machine.get("default_filament_profile")
    if isinstance(default, list):
        default = default[0] if default else None
    return _default_first(_compatible_names(index, "filament", machine_name), default)


# --------------------------------------------------------------------------- #
# Manifest -> per-tile print quantities
# --------------------------------------------------------------------------- #

def read_manifest(folder):
    """Load ``manifest.json`` rows from an export folder (``[]`` if absent)."""
    path = Path(folder) / "manifest.json"
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def tile_counts(rows):
    """``{stl_filename: copies}`` — how many of each unique STL to print.

    Identical tiles share one ``file`` in the manifest, so the count is simply
    how many rows reference it. STLs absent from the manifest default to 1 at
    lookup time (see :func:`count_for`).
    """
    counts = collections.Counter()
    for row in rows:
        fname = row.get("file")
        if fname:
            counts[str(fname)] += 1
    return dict(counts)


def count_for(counts, stl_filename):
    """Print quantity for an STL, defaulting to 1 when not in the manifest."""
    return counts.get(stl_filename, 1)


# --------------------------------------------------------------------------- #
# Output naming + G-code extraction
# --------------------------------------------------------------------------- #

def gcode_name(stl_stem, count):
    """Output G-code filename: ``<stem>.<count>.gcode``."""
    return f"{stl_stem}.{int(count)}.gcode"


def threemf_name(stl_stem, count):
    """Intermediate project filename: ``<stem>.<count>.gcode.3mf``."""
    return f"{stl_stem}.{int(count)}.gcode.3mf"


def extract_plate_gcode(threemf_path, dest_path):
    """Extract the plain plate G-code from a ``.gcode.3mf`` into ``dest_path``.

    Prefers ``Metadata/plate_1.gcode``; otherwise takes the first
    ``Metadata/plate_*.gcode`` member. Raises ``KeyError`` if the archive holds
    no plate G-code (e.g. the slice produced nothing).
    """
    with zipfile.ZipFile(threemf_path, "r") as zf:
        names = zf.namelist()
        target = "Metadata/plate_1.gcode"
        if target not in names:
            plates = sorted(
                n for n in names
                if n.startswith("Metadata/plate_") and n.endswith(".gcode")
            )
            if not plates:
                raise KeyError(f"no plate G-code inside {threemf_path}")
            target = plates[0]
        data = zf.read(target)
    with open(dest_path, "wb") as fh:
        fh.write(data)
    return dest_path


# --------------------------------------------------------------------------- #
# Full-config writing + slicing
# --------------------------------------------------------------------------- #

def write_full_configs(tmpdir, index, machine_name, process_name,
                       filament_name, density, pattern):
    """Flatten the three presets, inject infill overrides, write JSON configs.

    Returns ``(machine_json, process_json, filament_json)`` paths inside
    ``tmpdir``. The infill overrides land on the *process* config as
    ``sparse_infill_density = "<density>%"`` and ``sparse_infill_pattern``.
    """
    tmpdir = Path(tmpdir)

    machine = flatten_profile(index, "machine", machine_name)
    process = flatten_profile(index, "process", process_name)
    filament = flatten_profile(index, "filament", filament_name)

    process["sparse_infill_density"] = f"{int(density)}%"
    process["sparse_infill_pattern"] = pattern

    machine_json = tmpdir / "machine.json"
    process_json = tmpdir / "process.json"
    filament_json = tmpdir / "filament.json"
    for path, data in (
        (machine_json, machine),
        (process_json, process),
        (filament_json, filament),
    ):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    return str(machine_json), str(process_json), str(filament_json)


def build_slice_command(exe, stl_path, machine_json, process_json,
                        filament_json, outdir, out_3mf_name):
    """Assemble the Bambu Studio CLI argv for slicing one STL."""
    return [
        exe,
        "--load-settings", f"{machine_json};{process_json}",
        "--load-filaments", filament_json,
        "--arrange", "1",
        "--slice", "0",
        "--outputdir", str(outdir),
        "--export-3mf", out_3mf_name,
        str(stl_path),
    ]


def slice_one(exe, stl_path, machine_json, process_json, filament_json,
              outdir, out_3mf_name):
    """Slice one STL; return the path to the produced ``.gcode.3mf``.

    Raises ``RuntimeError`` on a non-zero exit or a missing output file, with
    the captured CLI output attached for diagnosis.
    """
    cmd = build_slice_command(
        exe, stl_path, machine_json, process_json, filament_json,
        outdir, out_3mf_name,
    )
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(outdir),
    )
    out_path = Path(outdir) / out_3mf_name
    if proc.returncode != 0 or not out_path.is_file():
        raise RuntimeError(
            f"Bambu Studio CLI failed (exit {proc.returncode}) for "
            f"{Path(stl_path).name}.\n--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return str(out_path)


def slice_folder(exe, index, folder, machine_name, process_name, filament_name,
                 density, pattern, log=print):
    """Slice every ``*.stl`` in ``folder``; write paired ``.gcode`` + ``.3mf``.

    Writes the flattened configs to a temp dir, derives per-tile counts from the
    manifest, and for each STL produces ``<stem>.<count>.gcode.3mf`` and the
    extracted ``<stem>.<count>.gcode``. ``log`` receives human-readable progress
    lines. Returns ``(ok_count, fail_count)``.
    """
    folder = Path(folder)
    stls = sorted(folder.glob("*.stl"))
    if not stls:
        log(f"No .stl files found in {folder}")
        return (0, 0)

    counts = tile_counts(read_manifest(folder))
    ok = fail = 0

    with tempfile.TemporaryDirectory(prefix="hexfinity_slice_") as tmp:
        machine_json, process_json, filament_json = write_full_configs(
            tmp, index, machine_name, process_name, filament_name,
            density, pattern,
        )
        log(f"Slicing {len(stls)} tile(s) with {machine_name} / "
            f"{process_name} / infill {int(density)}% {pattern}")
        for stl in stls:
            count = count_for(counts, stl.name)
            out_3mf = threemf_name(stl.stem, count)
            try:
                slice_one(exe, stl, machine_json, process_json, filament_json,
                          folder, out_3mf)
                extract_plate_gcode(folder / out_3mf,
                                    folder / gcode_name(stl.stem, count))
                ok += 1
                log(f"  OK  {stl.name}  ->  {gcode_name(stl.stem, count)} "
                    f"(x{count})")
            except (RuntimeError, KeyError, OSError) as exc:
                fail += 1
                log(f"  FAIL {stl.name}: {exc}")

    log(f"Done. {ok} sliced, {fail} failed.")
    return (ok, fail)


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


def normalize_pattern(value):
    """Map a settings ``sparse_infill_pattern`` to a Bambu internal value.

    Accepts either the internal value (``"honeycomb"``) or the human label
    (``"Honeycomb"``), case-insensitively. Raises ``ValueError`` for an unknown
    pattern so a typo fails loudly instead of silently slicing with the wrong
    infill.
    """
    if not value:
        return DEFAULT_PATTERN
    text = str(value).strip()
    lowered = text.lower()
    for internal, label in INFILL_PATTERNS:
        if lowered == internal.lower() or lowered == label.lower():
            return internal
    valid = ", ".join(v for v, _ in INFILL_PATTERNS)
    raise ValueError(f"unknown sparse_infill_pattern {value!r}; valid: {valid}")


def clamp_density(value):
    """Clamp a density to ``[INFILL_DENSITY_MIN, INFILL_DENSITY_MAX]`` (int)."""
    return max(INFILL_DENSITY_MIN, min(INFILL_DENSITY_MAX, int(value)))


def resolve_selection(index, settings):
    """Turn JSON ``settings`` into concrete slicing arguments.

    Returns ``(machine_name, process_name, filament_name, density, pattern)``.
    Empty ``printer``/``nozzle``/``filament``/``quality`` auto-fall-back the way
    the old UI seeded its dropdowns: first printer, 0.4 nozzle (else first), and
    the machine's default filament/process presets. Raises ``ValueError`` with a
    clear message when a named selection does not exist.
    """
    printers = list_printers(index)
    if not printers:
        raise ValueError("No instantiable printer presets found in the install.")

    model = settings.get("printer") or sorted(printers)[0]
    if model not in printers:
        raise ValueError(
            f"printer {model!r} not found; available: {sorted(printers)}")
    nozzles = printers[model]

    nozzle = str(settings.get("nozzle") or "").strip()
    if not nozzle:
        nozzle = "0.4" if "0.4" in nozzles else sorted(nozzles)[0]
    if nozzle not in nozzles:
        raise ValueError(
            f"nozzle {nozzle!r} not available for {model!r}; "
            f"available: {sorted(nozzles)}")
    machine_name = nozzles[nozzle]

    filaments = list_filaments(index, machine_name)
    filament_name = settings.get("filament") or (filaments[0] if filaments else "")
    if filament_name and filament_name not in filaments:
        raise ValueError(
            f"filament {filament_name!r} not compatible with {model!r} "
            f"{nozzle}mm; available: {filaments}")

    processes = list_processes(index, machine_name)
    process_name = settings.get("quality") or (processes[0] if processes else "")
    if process_name and process_name not in processes:
        raise ValueError(
            f"quality {process_name!r} not compatible with {model!r} "
            f"{nozzle}mm; available: {processes}")

    density = clamp_density(settings.get("sparse_infill_density",
                                         INFILL_DENSITY_DEFAULT))
    pattern = normalize_pattern(settings.get("sparse_infill_pattern"))
    return machine_name, process_name, filament_name, density, pattern


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
    printers = list_printers(index)
    model = _match_printer(printers, printer)
    nozzles = printers.get(model, {}) if model else {}
    noz = str(nozzle or "").strip()
    if noz not in nozzles:
        noz = "0.4" if "0.4" in nozzles else (sorted(nozzles)[0] if nozzles else "")
    machine_name = nozzles.get(noz)

    filaments = list_filaments(index, machine_name) if machine_name else []
    qualities = list_processes(index, machine_name) if machine_name else []

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
    exe = find_bambu_executable()
    root_profiles = profiles_dir(exe) if exe else None
    if not root_profiles:
        log("ERROR: cannot refresh — Bambu Studio install or its bundled "
            "profiles (resources/profiles/BBL) were not found.")
        return 2
    index = build_profile_index(root_profiles)
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
    exe = find_bambu_executable()
    if not exe:
        log(f"ERROR: Bambu Studio CLI not found on PATH or at {_DEFAULT_WIN_EXE}.")
        return 2
    root_profiles = profiles_dir(exe)
    if not root_profiles:
        log("ERROR: Bambu Studio found but its bundled profiles "
            "(resources/profiles/BBL) are missing.")
        return 2

    folder = str(settings.get("export_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        where = f" (from {settings_path})" if settings_path else ""
        log(f"ERROR: export_folder {folder!r} is not a valid directory{where}.")
        return 2

    index = build_profile_index(root_profiles)
    try:
        machine_name, process_name, filament_name, density, pattern = \
            resolve_selection(index, settings)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 2

    log(f"Bambu Studio: {exe}")
    ok, fail = slice_folder(
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
