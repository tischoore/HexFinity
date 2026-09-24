"""Bambu Studio CLI slicing mechanics — profile flattening, command
building, and G-code extraction. **Must never import bpy** — imported
directly by `scripts/slice_tiles.py` (via a `sys.path` insertion, the same
pattern `tests/conftest.py` uses to reach this package's other bpy-free
modules) for the standalone CLI, and by `hexfinity/operators.py` for the
in-Blender "Export + Slice" button. Any behavior change belongs here, not
duplicated in both call sites.

Why this exists / the three CLI realities it works around
-----------------------------------------------------------
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
"""

from __future__ import annotations

import collections
import copy
import json
import os
import shutil
import subprocess
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
                 density, pattern, log=print, results=None):
    """Slice every ``*.stl`` in ``folder``; write paired ``.gcode`` + ``.3mf``.

    Writes the flattened configs to a temp dir, derives per-tile counts from the
    manifest, and for each STL produces ``<stem>.<count>.gcode.3mf`` and the
    extracted ``<stem>.<count>.gcode``. ``log`` receives human-readable progress
    lines. When ``results`` is given (a list), one ``(stl_path, ok, message)``
    tuple is appended per tile — ``ok`` is a bool, ``message`` is the extracted
    G-code path on success or the failure text on error; a caller (e.g. the
    in-Blender Export + Slice operator) uses this to know exactly which STLs
    actually turned into G-code, e.g. before deleting the intermediate STL.
    Returns ``(ok_count, fail_count)``.
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
                gcode_path = extract_plate_gcode(
                    folder / out_3mf, folder / gcode_name(stl.stem, count))
                ok += 1
                log(f"  OK  {stl.name}  ->  {gcode_name(stl.stem, count)} "
                    f"(x{count})")
                if results is not None:
                    results.append((stl, True, str(gcode_path)))
            except (RuntimeError, KeyError, OSError) as exc:
                fail += 1
                log(f"  FAIL {stl.name}: {exc}")
                if results is not None:
                    results.append((stl, False, str(exc)))

    log(f"Done. {ok} sliced, {fail} failed.")
    return (ok, fail)


# --------------------------------------------------------------------------- #
# Settings resolution (shared shape for the JSON file and the Blender dialog)
# --------------------------------------------------------------------------- #

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
    """Turn a settings dict into concrete slicing arguments.

    ``settings`` is any mapping with ``printer``/``nozzle``/``filament``/
    ``quality``/``sparse_infill_density``/``sparse_infill_pattern`` keys (the
    JSON settings file and the Blender ``HexFinitySliceProperties`` dialog both
    produce this same shape). Returns ``(machine_name, process_name,
    filament_name, density, pattern)``. Empty ``printer``/``nozzle``/
    ``filament``/``quality`` auto-fall-back the way the old UI seeded its
    dropdowns: first printer, 0.4 nozzle (else first), and the machine's
    default filament/process presets. Raises ``ValueError`` with a clear
    message when a named selection does not exist.
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
