# Slicing exports to G-code with Bambu Studio

Two ways to turn a HexFinity export into ready-to-print G-code with the
**Bambu Studio command line**: the **Export + Slice** button inside Blender
(one click, no separate STL step), or the standalone `scripts/slice_tiles.py`
CLI script for an already-exported folder. Both drive the same underlying
mechanics (`hexfinity/bambu_slicer.py`), so behavior — presets, infill
overrides, output naming — is identical either way.

## Slicing from inside Blender (Export + Slice)

The **Export + Slice** button sits in the panel's Export box, next to
**Export Tiles to STL**. Click it and a dialog opens combining both steps:

- **Export Location** — the destination folder + subfolder, same as
  **Export Tiles to STL**'s own directory picker.
- **Slice Settings** — `Printer` / `Nozzle` / `Filament` / `Quality` are real
  dropdowns, populated from the locally installed Bambu Studio. Each
  defaults to **NONE** (auto: first printer, 0.4mm nozzle, the printer's own
  default filament/quality) until you pick something; picking a Printer
  resets Nozzle/Filament/Quality back to NONE (and picking a Nozzle resets
  Filament/Quality), since a value valid for one printer/nozzle combination
  may not exist for another. A **Refresh Presets** button re-scans the
  install — the dropdowns are cached for responsiveness while the dialog is
  open, so installing new Bambu Studio content mid-session needs this to
  show up without restarting Blender. `Infill Density`/`Infill Pattern` and
  a **Delete STLs after slicing** checkbox (on by default) round out the
  settings — a tile whose slice fails always keeps its STL regardless of
  this checkbox, so there's always something to retry.

Clicking OK exports every tile (exactly like **Export Tiles to STL** — they
share the same underlying export code, so they can never drift apart), then
slices each resulting STL with the locally installed Bambu Studio, producing
the same paired `.gcode`/`.gcode.3mf` output per tile described below.

**These settings are read from and written to the same
`scripts/slice_tiles_settings.json` the standalone CLI script below uses** —
picking a printer here becomes the CLI script's own default the next time
you run it manually, and vice versa (one shared file, one source of truth,
by design). Every field change is written immediately (not just on OK), so
the dialog remembers the last value picked across `.blend` files and Blender
sessions alike, not just for as long as one `.blend`'s scene properties say
so. This means using the dialog **modifies that file on disk** — worth
knowing since it's a git-tracked file in a dev checkout, so picking presets
here will show up as an uncommitted change. On an installed extension (not a
dev checkout), `scripts/` doesn't exist at all (`deploy.ps1` only ever zips
`hexfinity/*`) — every dropdown then just shows NONE every time, with
nowhere to persist a choice.

The main tradeoff versus the standalone script: slicing runs synchronously
on Blender's main thread (the same `subprocess` call either way), so Blender
is unresponsive for the duration of each tile's slice. For a handful of
tiles this is barely noticeable; for a large map, the standalone script
(which you can run in a separate terminal while continuing to work in
Blender) may be more convenient.

## The standalone script

`scripts/slice_tiles.py` turns a HexFinity STL export folder into ready-to-print
G-code using the **Bambu Studio command line**. It is a standalone CPython
script (no `bpy`, no Blender). It has **no interactive UI** — every parameter is
read from a JSON settings file.

```
python scripts/slice_tiles.py [export_folder] [--settings path.json]
```

- `export_folder` (optional) overrides `export_folder` in the settings file.
- `--settings` (optional) points at a different JSON file. The default is
  `slice_tiles_settings.json` next to the script.

## What it does

1. Loads the settings JSON (see [Parameters](#parameters-the-settings-json)).
2. Locates the Bambu Studio CLI (on `PATH`, else the default Windows install).
3. Reads the bundled presets from `resources/profiles/BBL` and resolves the
   chosen Printer / Nozzle / Filament / Quality against them.
4. Reads `manifest.json` in the export folder to learn how many of each tile to
   print (identical tiles are deduplicated to one STL — see
   [docs/export.md](export.md)), and `flora_manifest.json` (if present) to learn
   which hex positions have finalized planted trees.
5. Groups the folder's STLs into **jobs** (`bambu_slicer.slice_jobs`) — one job
   per plate/G-code:
   - A hex position with one or more finalized trees gets its own job: its tile
     STL plus every one of its tree STLs, sliced **together** in one CLI call so
     `--arrange 1` auto-arranges them onto one shared plate — the trees print
     right next to their own hex, not on a separate plate/G-code. Printed once
     (`count = 1`), named after the position (`hex_q00_r00`, disambiguated with
     a `_2`/`_3`/... suffix in the rare case that a plain, dedup-shared tile
     file's own bare name collides with a different flora position's name).
   - Every other tile STL keeps the original one-job-per-file behaviour,
     printed `<count>` times (from how many manifest rows reference it —
     identical, flora-less tiles share one file and one job).
6. For each job, writes **two** files next to the STL(s):
   - `hex_q00_r00.<count>.gcode.3mf` — the Bambu project (printers ingest this
     natively),
   - `hex_q00_r00.<count>.gcode` — the plain G-code extracted from it.

   `<count>` is the print quantity. An STL not referenced by the manifest at
   all defaults to a count of `1`, sliced on its own.

**Object spacing during that auto-arrange** (e.g. a tree next to its hex) is
not something this script, `bambu_slicer.py`, or the CLI's config files
control — it is Bambu Studio's own saved "Auto-Arrange" spacing preference
(the small arrow/gear next to the Auto-Arrange toolbar button in the GUI,
persisted as `arrange.min_object_distance_fff` in your
`%APPDATA%\BambuStudio\BambuStudio.conf`). Set it once in the Bambu Studio
GUI (e.g. to `10`mm) and every future `--arrange 1` — from this script, the
in-Blender dialog, or the GUI itself — reuses that same saved value. HexFinity
deliberately never reads or writes that file itself: it's a large,
checksum-protected file holding many unrelated Bambu Studio preferences, and
getting the checksum format wrong (or overwriting an unrelated user setting)
risks corrupting preferences well beyond this one value.

**Verified empirically** (direct CLI test outside Blender, comparing
`--arrange 1` vs `--arrange 10` against the same two STLs): the numeric
argument has **no effect** on the resulting layout — both produced
byte-identical object transforms. The only thing that changed the gap was
the value already saved in `BambuStudio.conf`. Do not try passing a
different number here expecting a different mm spacing; it is a no-op on
this CLI build.

## Parameters (the settings JSON)

`slice_tiles_settings.json` (next to the script) holds every parameter. The
default file:

```json
{
  "export_folder": "",
  "sparse_infill_density": 15,
  "sparse_infill_pattern": "honeycomb",
  "adaptive_layer_height": false,
  "printer": "",
  "nozzle": "",
  "filament": "",
  "quality": ""
}
```

| Key | Meaning |
|---|---|
| `export_folder` | Folder of STLs + `manifest.json` produced by HexFinity. The positional CLI argument overrides this. |
| `sparse_infill_density` | 5–20% (clamped). Written as `sparse_infill_density`. |
| `sparse_infill_pattern` | Bambu pattern — internal value (`"honeycomb"`) **or** human label (`"3D Honeycomb"`), case-insensitive. An unknown value fails loudly. Default **Honeycomb**. |
| `adaptive_layer_height` | Bambu Studio's adaptive layer height (bool). Written onto the process config as `adaptive_layer_height = "1"`/`"0"`. Default off. |
| `printer` | Printer model name, e.g. `"Bambu Lab P1S"`. Empty → the first instantiable printer in the install. |
| `nozzle` | Nozzle diameter for that model (`"0.2"` / `"0.4"` / `"0.6"` / `"0.8"`). Together with `printer` it selects one machine preset. Empty → `0.4` when available, else the first. |
| `filament` | Compatible filament preset name. Empty → the printer's `default_filament_profile` (Bambu PLA Basic). |
| `quality` | Compatible process/quality preset name. Empty → the printer's `default_print_profile` (≈ 0.20mm Standard). The infill settings above override this base process. |

Leaving the four preset keys (`printer`/`nozzle`/`filament`/`quality`) as `""`
reproduces the old UI's auto-seeded defaults. Naming a preset that does not
exist in the install fails with the list of valid options. Progress (one line
per tile, plus a final summary) is printed to the console; the exit code is `0`
when nothing failed, `1` when at least one tile failed, `2` on a setup/settings
error.

### `possible_values` — the reference block

The settings file also carries an optional `possible_values` object that **does
nothing at slice time** (it is stripped before slicing). It documents, for each
setting, every valid value or the numeric range:

| Key | Form |
|---|---|
| `export_folder` | A note (any directory is allowed). |
| `sparse_infill_density` | `{ "min": 5, "max": 20, "unit": "percent" }`. |
| `sparse_infill_pattern` | The full list of internal pattern values. |
| `adaptive_layer_height` | `[false, true]`. |
| `printer` | Every installed printer model. |
| `nozzle` | The selected printer's nozzle diameters. |
| `filament` | Every compatible filament preset for the selected printer+nozzle. |
| `quality` | Every compatible process/quality preset for the selected printer+nozzle. |

`filament` and `quality` depend on the chosen printer, so the block is a
snapshot. Regenerate it from the live install at any time — it rewrites only the
`possible_values` block, leaving your real settings untouched:

```
python scripts/slice_tiles.py --refresh
```

The block's `_note` field records which printer/nozzle its `filament`/`quality`
lists were enumerated for.

## How it works around the Bambu CLI's three realities

The Bambu Studio CLI is capable of everything above, but not directly — the
script bridges three gaps:

1. **No direct `.gcode` output.** The CLI only writes `.gcode.3mf` (via
   `--export-3mf`). Plain G-code is the member `Metadata/plate_1.gcode` *inside*
   that zip; the script extracts it (keeping both files).
2. **No per-setting CLI flags.** There is no generic `--key=value` override.
   Printer, nozzle, filament and the infill/layer settings are all supplied as
   full-config JSON files via `--load-settings "machine.json;process.json"` and
   `--load-filaments "filament.json"`.
3. **Shipped presets are partial.** Presets under `resources/profiles/BBL` use
   `"inherits"` (and machine presets use `"include"` for G-code templates), so
   they are not valid full configs. The script **flattens** each chosen preset's
   inheritance chain into a complete config, injects
   `sparse_infill_density` / `sparse_infill_pattern` / `adaptive_layer_height`
   onto the process config, writes the three JSON files to a temp dir, then
   calls the CLI.

Supports are always forced on: `write_full_configs` unconditionally sets
`enable_support = "1"` on the flattened process config, regardless of what the
chosen quality preset's own default is (every shipped Bambu process preset
inherits `enable_support = "0"` from `fdm_process_common.json`). This is not a
settings-file key — there is no opt-out — and it applies to every STL a
`slice_folder` run touches, tile and flora alike, since they all share the one
flattened process config.

The resulting command per job (a flora-less tile passes just the one STL; a
flora-attached hex passes its tile *and* every one of its tree STLs together)
is:

```
bambu-studio --load-settings "machine.json;process.json" \
             --load-filaments "filament.json" \
             --arrange 1 --slice 0 \
             --outputdir <export_folder> --export-3mf <name>.gcode.3mf \
             <tile>.stl [<tree01>.stl <tree02>.stl ...]
```

## Verifying a run

- Each job should produce a paired `.gcode` and `.gcode.3mf` with the same
  `.<count>.` infix; a flora-less tile's count should match the manifest's
  tile grouping, and a flora-attached hex's job is always `.1.` (printed once,
  tile and trees together).
- Open a produced `.gcode` and check the trailing config block contains your
  chosen `sparse_infill_density = N%`, `sparse_infill_pattern = <pattern>`,
  (if enabled) `adaptive_layer_height = 1`, and `enable_support = 1` (always,
  unconditionally) — that proves the flattened-config overrides reached the
  slicer.
- For a flora-attached hex, open `Metadata/model_settings.config` (or
  `3D/3dmodel.model`) inside the `.gcode.3mf` and confirm both the tile and
  its tree(s) are present as separate `<object>`/`<item>` entries with
  distinct `transform` positions — that proves they arranged onto one shared
  plate rather than one overwriting the other.

## Troubleshooting

- **"Bambu Studio CLI not found"** — install Bambu Studio, or add `bambu-studio`
  to `PATH`. The script also checks `C:\Program Files\Bambu Studio\bambu-studio.exe`.
- **"bundled profiles … are missing"** — the executable was found but its
  `resources/profiles/BBL` folder is missing; reinstall Bambu Studio.
- **"export_folder … is not a valid directory"** — set `export_folder` in the
  settings JSON (or pass a folder argument) to an existing export folder.
- **"unknown setting(s) …" / "unknown sparse_infill_pattern …" / "printer …
  not found"** — a typo in the settings JSON; the message lists the valid keys
  or values.
- **A tile reports `FAIL`** — the log includes the CLI's stdout/stderr. The most
  common cause is an incomplete config; the script flattens `inherits` *and*
  machine `include`, but a heavily customized/3rd-party preset may reference
  something not present locally. Try a stock Bambu printer/quality preset.
- **`no plate G-code inside …`** — the slice produced no toolpaths (e.g. the
  model fell outside the bed). Check the STL opens and fits the selected printer.

## Tests

The pure slicing mechanics (manifest counts, naming, inheritance flattening,
infill override injection, G-code extraction, command construction, settings
resolution, settings-JSON load/write/merge, the dropdown-dependency
`resolve_machine` helper, and the enumeration cache) live in
`hexfinity/bambu_slicer.py` and are covered by `tests/test_bambu_slicer.py`:

```
"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest tests -v
```

The `possible_values` reference-doc block and `run()`/`main()`/
`refresh_settings()` wiring — what's still local to `scripts/slice_tiles.py`
— is covered by `scripts/tests/test_slice_tiles.py`:

```
"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest scripts/tests -v
```

Both are `bpy`-free and pass under any CPython. The in-Blender **Export +
Slice** operator itself (dialog, subprocess call, panel button) has no
automated coverage — like every other bpy-shell operator in this codebase,
it's exercised by a manual checklist script instead:
`tests/_headless_export_and_slice_check.py`, run via
`blender --background --python tests/_headless_export_and_slice_check.py`.
