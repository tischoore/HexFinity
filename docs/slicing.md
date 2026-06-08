# Slicing exports to G-code with Bambu Studio

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
   [docs/export.md](export.md)).
5. For every `*.stl` in the folder, slices it and writes **two** files next to
   the STL:
   - `hex_q00_r00.<count>.gcode.3mf` — the Bambu project (printers ingest this
     natively),
   - `hex_q00_r00.<count>.gcode` — the plain G-code extracted from it.

   `<count>` is the print quantity. STLs that are not in the manifest default to
   a count of `1`.

## Parameters (the settings JSON)

`slice_tiles_settings.json` (next to the script) holds every parameter. The
default file:

```json
{
  "export_folder": "",
  "sparse_infill_density": 15,
  "sparse_infill_pattern": "honeycomb",
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
   Printer, nozzle, filament and the infill settings are all supplied as
   full-config JSON files via `--load-settings "machine.json;process.json"` and
   `--load-filaments "filament.json"`.
3. **Shipped presets are partial.** Presets under `resources/profiles/BBL` use
   `"inherits"` (and machine presets use `"include"` for G-code templates), so
   they are not valid full configs. The script **flattens** each chosen preset's
   inheritance chain into a complete config, injects
   `sparse_infill_density` / `sparse_infill_pattern` onto the process config,
   writes the three JSON files to a temp dir, then calls the CLI.

The resulting command per tile is:

```
bambu-studio --load-settings "machine.json;process.json" \
             --load-filaments "filament.json" \
             --arrange 1 --slice 0 \
             --outputdir <export_folder> --export-3mf <name>.gcode.3mf \
             <tile>.stl
```

## Verifying a run

- Each STL should produce a paired `.gcode` and `.gcode.3mf` with the same
  `.<count>.` infix; counts should match the manifest's tile grouping.
- Open a produced `.gcode` and check the trailing config block contains your
  chosen `sparse_infill_density = N%` and `sparse_infill_pattern = <pattern>` —
  that proves the flattened-config overrides reached the slicer.

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

The pure logic (manifest counts, naming, inheritance flattening, infill
override injection, G-code extraction, command construction) is covered by
`scripts/tests/test_slice_tiles.py`:

```
"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pytest scripts/tests -v
```

These are `bpy`-free and pass under any CPython.
