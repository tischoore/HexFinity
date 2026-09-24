# scripts/

Standalone command-line utilities that support the HexFinity workflow but run
outside Blender's UI — batch STL re-centering, batch G-code slicing, and
batch STL rescaling. `stl_center.py` needs Blender itself (it uses
`bpy.ops.wm.stl_import`/`stl_export`); `slice_tiles.py` and `scale_stl.py`
are plain CPython and run with any Python 3, no Blender required.

Each bpy-free script's pure logic is covered by pytest in `scripts/tests/`:

```
python -m pytest scripts/tests -v
```

(Blender's bundled Python doesn't ship pytest — install it once first, per
the root `README.md`'s "Running the unit tests" section.)

## slice_tiles.py

Batch-slices a HexFinity STL export folder to G-code with a locally
installed **Bambu Studio**, naming each output with its print quantity:

```
python scripts/slice_tiles.py [export_folder] [--settings path.json]
```

Every parameter (export folder, infill density/pattern, printer, nozzle,
filament, quality) is read from `scripts/slice_tiles_settings.json` — there
is no interactive UI. See **[../docs/slicing.md](../docs/slicing.md)** for
the full parameter reference, the Bambu CLI caveats it works around, and
troubleshooting.

The reusable slicing mechanics (profile flattening, command building, G-code
extraction, settings resolution) live in `hexfinity/bambu_slicer.py` — a
bpy-free module also used by the in-Blender **Export + Slice** button (see
docs/slicing.md), so a change to slicing *behavior* belongs there, not
duplicated here. This script keeps only the settings-JSON/CLI-specific glue.

## stl_center.py

Batch re-centers STL origins to their bounding-box center (or bottom),
translating each mesh in place — never rotating or scaling it. Needs
Blender, since it goes through `bpy.ops.wm.stl_import`/`stl_export`:

```
"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" ^
    --background --python scripts\stl_center.py -- <folder> [-r] [-b]
```

- `<folder>` — folder of `.stl` files to re-center.
- `-r` / `--recursive` — recurse into subfolders (default: top level only).
- `-b` / `--bottom` — place the new origin's Z at the mesh's lowest point
  instead of the bounding-box center (useful for assets meant to sit flush
  on a surface).

Unlike `scale_stl.py` below, this **overwrites each file in place** — there
is no output directory or filename prefix.

## scale_stl.py

Batch-scales STL files uniformly, writing prefixed copies into an output
directory — for rescaling an already-exported print between scales (e.g.
28 mm ↔ 10 mm figure scale) without regenerating the map in Blender at a
different Man Height. Plain CPython, no Blender required:

```
python scripts/scale_stl.py <input> <scale> --prefix PREFIX -o OUTPUT_DIR [--force] [--recursive]
```

| Argument | Required | Meaning |
|---|---|---|
| `input` | yes | A single `.stl` file, or a folder of `.stl` files. |
| `scale` | yes | Uniform scale factor (float, must be `> 0`). |
| `--prefix` | yes | Prepended verbatim to each output filename, e.g. `10mm_`. |
| `-o`, `--output` | yes | Output folder for the scaled copies (created if missing). |
| `--force` | no | Overwrite an existing output file instead of skipping it. |
| `-r`, `--recursive` | no | When `input` is a folder, recurse into subfolders (default: top level only). |

**What it does:** detects whether each input file is binary or ASCII STL,
scales every vertex by `scale` (surface normals are left unchanged — valid
because scaling is uniform), and always writes the result as **binary**
STL — matching the format Blender's own `wm.stl_export` produces elsewhere
in this repo, regardless of whether the input was binary or ASCII.

### Worked example: 28mm scale → 10mm scale (factor 0.357)

```
python scripts/scale_stl.py export/ 0.357 --prefix 10mm_ -o export_10mm/
```

28 mm is HexFinity's "common wargaming scale" Man Height reference (see the
root `README.md`'s Terrain section). `0.357` is a rounded convenience
factor for `10/28` (≈ 0.3571), not exact — if exact precision matters,
regenerating the map in Blender at `man_height_mm = 10` is the precise
path. This script is for quickly rescaling an *existing* export instead of
regenerating one.

### Worked example: 10mm scale → 28mm scale (factor 2.8)

```
python scripts/scale_stl.py export_10mm/ 2.8 --prefix 28mm_ -o export_28mm/
```

The inverse direction: `2.8 = 28/10` exactly.

### Edge cases / limitations

- Output is **always binary**, even when the input was ASCII — there is no
  ASCII output mode.
- An existing output file is skipped (with a warning), not overwritten,
  unless `--force` is passed.
- Non-`.stl` files in an input folder are ignored; an empty folder produces
  no output and exits successfully.
- A truncated/corrupt binary STL, or an ASCII STL with no facets, is
  skipped (reported per-file) rather than aborting the whole batch.
- No geometry validation is performed — this is a scaling transform, not a
  manifold checker (see `hexfinity/manifold_check.py` for that, which only
  applies to tiles built by the Blender add-on itself).
- `--prefix ""` (empty string) is allowed — the output filename would then
  equal the input filename, which only matters if `input` and `-o` point at
  the same folder.
- A pathological ASCII file whose byte size happens to exactly match the
  binary-STL size formula would be misdetected as binary and fail to parse
  — the same known, extremely-unlikely edge case other STL tooling accepts.
