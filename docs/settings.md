# Settings (`settings.json`)

HexFinity keeps one small piece of persistent state outside the `.blend`
file: `settings.json`, the library backing the **Add Path Segment Type**
authoring tool (see the [Settings section of the README](../README.md#settings)).
It records externally-authored path-segment STLs (bridges, junctions,
etc.), grouped by type, each with the connector waypoints drawn for it —
consumed by the **Draw Segments Path** tool (see the
[Draw Segments Path section of the README](../README.md#draw-segments-path))
to place them onto the map. This document is the schema reference; it
does not describe that consumer's own placement/snapping/boolean-clipping
behaviour.

## Where it lives

`settings.json` is stored in Blender's own per-user, per-extension data
directory, resolved at runtime via `bpy.utils.extension_path_user(...)` —
**not** inside HexFinity's installed extension folder, which Blender may
overwrite or reset on update/reinstall. On Windows this typically resolves
to something like:

```
%APPDATA%\Blender Foundation\Blender\5.1\extensions\.local\hexfinity\settings.json
```

The exact path depends on your Blender version and how the extension was
installed. The file is created automatically (with an empty default
document) the first time HexFinity registers, if it doesn't already exist,
and re-parsed on every subsequent load — you never need to create it by
hand.

The file is machine-managed by the Add Path Segment Type workflow. You
*can* hand-edit it, but it's unsupported — back it up first, and keep the
JSON valid (a malformed file is reported as an error rather than silently
replaced, so HexFinity won't discard your data, but it also won't recover
it for you).

## Schema

```json
{
  "schema_version": 1,
  "last_directory": "C:/Users/andre/HexSegments/Bridge",
  "segment_path": {
    "types": {
      "Bridge": {
        "segments": [
          {
            "file": "C:/Users/andre/HexSegments/Bridge/bridge_01.stl",
            "man_height_mm": 10.0,
            "hull_local_mm": [[-40.0, -12.5], [40.0, -12.5], [40.0, 12.5], [-40.0, 12.5]],
            "corners_local_mm": [[-40.0, -12.5], [40.0, -12.5], [40.0, 12.5], [-40.0, 12.5]],
            "waypoints": [
              {"x_mm": -40.0, "y_mm": 0.0, "z_mm": 12.0, "edge_idx": 3},
              {"x_mm": 0.0,   "y_mm": 0.0, "z_mm": 12.0, "edge_idx": -1},
              {"x_mm": 40.0,  "y_mm": 0.0, "z_mm": 12.0, "edge_idx": 1}
            ],
            "is_end_segment": false,
            "edge_snap": 3,
            "added_utc": "2026-09-20T12:34:56Z"
          }
        ]
      }
    }
  }
}
```

| Field | Meaning |
|---|---|
| `schema_version` | Integer, bumped only if the shape below ever changes incompatibly. |
| `last_directory` | The folder the file browser last opened an STL from — pure UX convenience (pre-fills the file browser next time), not otherwise read by any feature. |
| `segment_path.types` | Map of **type name → type entry**. |

### Type entry

| Field | Meaning |
|---|---|
| `segments` | List of segment entries (below) belonging to this type. |

**Type name assumption**: a type's name is always the STL's *parent
folder* name, taken verbatim, and assumed unique — two different folders
named e.g. `Bridge` on different drives would collide into the same type.
This is a deliberate simplification, not a validated constraint.

### Segment entry

| Field | Meaning |
|---|---|
| `file` | Absolute path to the original STL. The file is **referenced**, not copied — HexFinity does not duplicate it into its own storage, so moving/renaming/deleting the source file breaks this entry. |
| `man_height_mm` | Always `10.0`. A fixed documentation label recording the scale assumption every segment is authored under — **not enforced or auto-rescaled** by HexFinity. If your STL wasn't modeled at 10 mm man-height, its geometry will be wrong relative to a hex tile's own scale; there is no correction applied. |
| `hull_local_mm` | The convex hull of the STL's footprint (its vertices projected to XY), in the segment's own local mm space, ordered counter-clockwise. Captured once at authoring time so a future consumer never needs to recompute it from the mesh. |
| `corners_local_mm` | The user-authored **Define Corners** polygon (see the README's Settings section), same `[[x, y], ...]` shape as `hull_local_mm` but placed by hand via the "Add Corner" tool rather than computed from the mesh — may be concave, unlike `hull_local_mm`. Like `hull_local_mm`, this is write-only today: nothing reads it back out of a loaded `settings.json` yet (a future reader should use `.get("corners_local_mm", [])`, since segments saved before this field existed won't have it). |
| `waypoints` | The drawn connector path: a list of `{x_mm, y_mm, z_mm, edge_idx}` points in the same local mm space as `hull_local_mm`. `z_mm` starts out at the draw tool's click-plane height (the segment's tallest vertex plus clearance) and is only ever refined by hand-editing it in the authoring panel's waypoint list — nothing currently reads it back (the Draw Segments Path consumer only matches connectors by `x_mm`/`y_mm`/`edge_idx`). |
| `is_end_segment` | `true` if exactly one waypoint has `edge_idx >= 0` (see below) — a segment with only one connection point, e.g. a dead end or terminus, rather than a through-piece. |
| `edge_snap` | The Edge Snap density (points per hull edge) the waypoints were drawn with — kept for reference/reproducibility, not re-validated against `hull_local_mm` later. |
| `added_utc` | UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`) of when this segment was added. |

### Waypoint `edge_idx`

Each waypoint is tagged with which hull edge (if any) it snapped to:

- `edge_idx >= 0` — the waypoint sits on hull edge `edge_idx` (indexing into `hull_local_mm`: edge `i` runs from vertex `i` to vertex `(i+1) % len(hull_local_mm)`). This marks a **connection point** — where this segment is meant to join another piece or continue a path.
- `edge_idx == -1` — an interior point (not on the footprint's edge), just part of the path's shape.

**At least one waypoint must have `edge_idx >= 0`, and `corners_local_mm` must have at least 3 points** — the authoring tool refuses to finish a segment missing either: a segment that connects to nothing has no way to be spliced into anything later, and a footprint polygon needs at least 3 points to be a polygon at all.

## Duplicate detection

Adding a segment checks for an existing entry under the *same type* whose
`file` matches (case-insensitively, by absolute path) — the same file
can't be registered twice under one type. The same file *can* be
registered under two different types, since a type is just a folder-name
label, not a claim of exclusive ownership.
