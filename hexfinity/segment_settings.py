"""Pure-Python (bpy-free) settings.json schema, read/write, and dedup logic
for the Path Segment authoring tool. No `bpy` import, so this stays
unit-testable in plain CPython — the bpy-importing shell (file dialogs, STL
import, viewport setup, the Draw Path modal) lives in segments.py, which
resolves the on-disk path (via bpy.utils.extension_path_user) and calls
into this module for everything else.
"""

import json
import os
import tempfile
from datetime import datetime, timezone


SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

DEFAULT_MAN_HEIGHT_MM = 10.0


class SettingsError(Exception):
    """Raised for a settings.json that can't be parsed or has an
    unrecoverable shape. Callers must report this, never silently overwrite
    a broken user file with a fresh default."""


class DuplicateSegmentError(SettingsError):
    """Raised by add_segment() when the given file is already registered
    under the given type."""


def default_settings():
    """A brand-new, empty settings.json document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "last_directory": None,
        "segment_path": {"types": {}},
    }


def validate_settings(data):
    """Normalize/repair missing top-level keys in-place-ish (returns the
    possibly-patched dict); raises SettingsError on a shape that can't be
    recovered (e.g. `types` present but not a dict)."""
    if not isinstance(data, dict):
        raise SettingsError("settings.json root must be a JSON object")

    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("last_directory", None)

    segment_path = data.setdefault("segment_path", {})
    if not isinstance(segment_path, dict):
        raise SettingsError("settings.json 'segment_path' must be an object")

    types = segment_path.setdefault("types", {})
    if not isinstance(types, dict):
        raise SettingsError("settings.json 'segment_path.types' must be an object")

    for type_name, type_entry in types.items():
        if not isinstance(type_entry, dict):
            raise SettingsError(f"segment type {type_name!r} must be an object")
        segments = type_entry.setdefault("segments", [])
        if not isinstance(segments, list):
            raise SettingsError(f"segment type {type_name!r} 'segments' must be a list")

    return data


def load_settings(path):
    """Load settings.json at `path`. A missing file returns a fresh
    default_settings() (never written to disk here — that's the caller's
    call, typically via save_settings once something is actually added).
    A present-but-malformed file raises SettingsError rather than silently
    discarding the user's data."""
    if not os.path.isfile(path):
        return default_settings()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"could not read {path}: {exc}") from exc

    return validate_settings(data)


def save_settings(path, data):
    """Serialize `data` to `path` atomically: write to a temp file in the
    same directory, then os.replace() over the target, so a crash mid-write
    (or a concurrent Blender instance) never leaves a truncated/corrupt
    settings.json."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def get_last_directory(data):
    return data.get("last_directory")


def set_last_directory(data, directory):
    data["last_directory"] = directory


def _types(data):
    return data["segment_path"]["types"]


def has_type(data, type_name):
    return type_name in _types(data)


def normalize_path(path):
    """Case-insensitive-on-Windows, absolute form used for all file-identity
    comparisons in this module."""
    return os.path.normcase(os.path.abspath(path))


def find_segment(data, type_name, filepath):
    """The existing segment entry for `type_name` whose `file` matches
    `filepath` (normalized comparison), or None. Never matches across
    different types."""
    type_entry = _types(data).get(type_name)
    if type_entry is None:
        return None
    target = normalize_path(filepath)
    for segment in type_entry["segments"]:
        if normalize_path(segment["file"]) == target:
            return segment
    return None


def add_type(data, type_name):
    """Idempotent: ensures segment_path.types[type_name] exists."""
    _types(data).setdefault(type_name, {"segments": []})


def add_segment(data, type_name, filepath, hull_local_mm, corners_local_mm, waypoints,
                 is_end_segment, edge_snap, man_height_mm=DEFAULT_MAN_HEIGHT_MM):
    """Ensure `type_name` exists, then append a new segment entry for
    `filepath` and return it. Raises DuplicateSegmentError if this file is
    already registered under this type (checked by normalized path).

    corners_local_mm mirrors hull_local_mm's shape (a list of (x, y) pairs)
    but records the segment's user-authored Corners polygon (see
    segments.HEXFINITY_OT_add_corner) rather than the auto-computed convex
    hull. Like hull_local_mm, it is write-only today — nothing reads it
    back out of a loaded settings.json yet, so a future reader must use
    `.get('corners_local_mm', [])` since older entries won't have the key
    at all (no schema_version bump/migration was added for this reason)."""
    if find_segment(data, type_name, filepath) is not None:
        raise DuplicateSegmentError(
            f"{filepath!r} is already registered under type {type_name!r}")

    add_type(data, type_name)
    entry = {
        "file": filepath,
        "man_height_mm": man_height_mm,
        "hull_local_mm": [[x, y] for (x, y) in hull_local_mm],
        "corners_local_mm": [[x, y] for (x, y) in corners_local_mm],
        "waypoints": [
            {"x_mm": x, "y_mm": y, "z_mm": z, "edge_idx": edge_idx}
            for (x, y, z, edge_idx) in waypoints
        ],
        "is_end_segment": is_end_segment,
        "edge_snap": edge_snap,
        "added_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _types(data)[type_name]["segments"].append(entry)
    return entry


def list_types(data):
    """Sorted list of every segment type name currently in `data`."""
    return sorted(_types(data).keys())


def list_segments(data, type_name):
    """The raw list of segment entry dicts registered under `type_name`, in
    on-disk order, or [] if the type doesn't exist."""
    return _types(data).get(type_name, {}).get("segments", [])


def remove_segment(data, type_name, index):
    """Remove and return the segment entry at `index` within `type_name`'s
    list. Raises SettingsError for an unknown type or an out-of-range
    index. Deliberately leaves the type entry in place (with an empty
    segments list) if this was its last segment — deleting the type itself
    isn't requested and would destabilize a caller's dropdown mid-use."""
    if not has_type(data, type_name):
        raise SettingsError(f"unknown segment type {type_name!r}")
    segments = _types(data)[type_name]["segments"]
    if not (0 <= index < len(segments)):
        raise SettingsError(f"segment index {index} out of range for type {type_name!r}")
    return segments.pop(index)


def move_segment(data, type_name, index, direction):
    """Swap the segment entry at `index` within `type_name`'s list with its
    neighbour at `index + direction` (direction is -1 for up/earlier, +1
    for down/later). Returns True if the swap happened, or False (no
    mutation) if it would move past either end of the list — mirroring how
    Blender's own list-reorder operators silently no-op at a boundary
    rather than raising. Raises SettingsError for an unknown type or an
    out-of-range `index`."""
    if not has_type(data, type_name):
        raise SettingsError(f"unknown segment type {type_name!r}")
    segments = _types(data)[type_name]["segments"]
    if not (0 <= index < len(segments)):
        raise SettingsError(f"segment index {index} out of range for type {type_name!r}")
    target = index + direction
    if not (0 <= target < len(segments)):
        return False
    segments[index], segments[target] = segments[target], segments[index]
    return True
