"""Path feature (footpath/track/road) authoring — draw an open waypoint
line above a tile, then auto-carve it into the tile's top surface.

A feature is an open polyline (tile-local mm) stored in the tile's
`path_features` CollectionProperty, plus a type. This module contains both
the bpy authoring UI — a modal waypoint picker (mirrors `regions.py`'s
point picker, but places points on a flat "man height" plane above the tile
instead of raycasting onto the mesh) plus a remove operator and the list UI
— and the bpy texture-asset pipeline (`PATH_TEXTURES`/`_get_or_load_heightmap`)
that turns a line into `mesh_builder.build_hex_tile`'s `path_features` kwarg
via `path_specs()`. The actual curvilinear-sampling math is bpy-free, in
`tree_pads.refine_and_displace_along_path`.

A click that snaps to an existing line's waypoint (on the same tile) always
ends the line there, same as ever. A click that snaps to one of the tile's
own hex-edge points (`map.edge_snap_points`) either ends the line there
(if the neighbour tile across that edge isn't selected, or doesn't exist) or
*continues the line onto that neighbour tile* (if it is selected) — the
line's current segment is committed to the tile being left, and a brand-new
segment, seeded with the shared edge point and the just-committed segment's
settings, starts on the neighbour. This is how a single drawing gesture
spans multiple hexes: select every hex the path should cross (normal
multi-select) before starting Draw Feature. Each spanned hex ends up owning
its own independent path feature, sharing only a coincident waypoint with
its neighbour's feature — the same "each hex is self-contained" model
`HEXFINITY_OT_link_connected_paths` already assumes when syncing settings
across a shared endpoint. Crossing recentres the viewport on the new hex,
keeping the camera's rotation/distance unchanged.

Every edit (drawing a line, changing its type/width/depth/repeat/texture,
removing it) auto-rebuilds the tile — there is no manual "Generate" step,
matching every other generative feature in the codebase.

RIVER is a fourth feature_type with its own field shape (depth in map
Levels, an embankment angle + variation, a Flat/Tessendorf's-FFT bottom
style, a Preserve Edge toggle, no texture/repeat) and its own carve math
(tree_pads.refine_and_carve_river) — see _RIVER_DEFAULTS/path_specs()
below. By default (Preserve Edge on), like every other pad/path/brush
displacement in this codebase, a river's depth fades to exactly 0 at the
tile's rim edge (the invariant that keeps two independently-built
neighbouring tiles' shared edges matching). A river meant to continue into
a neighbouring tile then needs the shared corner Level(s) at the crossing
edge lowered to at least the river's own depth_levels on both tiles (the
per-corner Level sliders already auto-propagate to the shared neighbour
corner) — this makes the ambient terrain the rim-fade reverts to already
sit at the river's bed depth, so the seam reads as continuous with no
special-case code required. Turning a river's own Preserve Edge off is the
other way to get there: with a waypoint on the hex edge, it carves right up
to the rim with a deterministic (unvaried) embankment there instead of
fading back to ambient, so a matching river on the neighbouring tile (same
waypoint position, same width/depth/angle, also Preserve Edge off) can
continue it directly, without touching corner Levels at all.
"""

import math
import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

from .map import (edge_snap_points, point_in_hex, find_connected_component,
                  EDGE_DIRECTIONS, neighbour_coord, find_tile)


SNAP_RADIUS_PX = 18.0
# How long a hex-crossing's viewport recentre takes to glide, in seconds --
# fixed rather than user-configurable, matching SNAP_RADIUS_PX's own
# hardcoded-constant convention for a purely cosmetic value.
VIEW_PAN_DURATION_S = 1.2
_LINE_COLOR = (0.85, 0.55, 0.25, 0.9)
_SNAP_COLOR = (0.3, 1.0, 0.5, 0.95)
_POINT_COLOR = (1.0, 1.0, 1.0, 1.0)

# Settings fields propagated by "Link Connected Paths" -- every physical/
# carve field, deliberately excluding `name` (kept per-path) and `points`
# (never touch waypoints/geometry, only settings). feature_type is included
# and handled specially (see HEXFINITY_OT_link_connected_paths.execute):
# copying it is exactly what makes a multi-hex road's type uniform.
_PATH_FEATURE_LINK_FIELDS = (
    "feature_type", "width_mm", "depth_mm", "repeat_mm", "local_subdiv",
    "texture", "depth_levels", "embankment_angle_deg",
    "embankment_variation_mm", "river_bottom_style", "preserve_edge",
)

# Generous vs. float32 property-storage/matrix-round-trip noise at hex-scale
# (mm) coordinates, far below the real spacing between distinct edge_snap
# candidates -- see map.find_connected_component's docstring for the shape
# of the comparison this feeds.
_LINK_EPSILON_MM = 0.01

# Working resolution every cached heightmap is downsampled to before pixel
# extraction. The source art is 4K (~67M pixels); a groove profile only
# ever needs on the order of tens-to-low-hundreds of samples across a
# path's width/length, so caching the full resolution would be a needless
# memory/time cost paid on every load.
HEIGHTMAP_WORKING_RES = 512

# key -> {"label", "file"} — "file" is relative to this package's
# assets/Path Features/ directory; None means the flat fallback (Part B of
# the plan: refine_and_displace_along_path treats a pad with no pixels as a
# uniform full-depth groove, not a no-op).
PATH_TEXTURES = {
    "NONE": {"label": "None (flat)", "file": None},
    "STONE_ROAD": {"label": "Stone Road", "file": "stone_road_disp_4k.png"},
    "BRICK_GRAVEL": {"label": "Brick Gravel", "file": "brick_gravel_disp_4k.png"},
}

# feature_type -> width factor (of man_height_mm) + literal depth/repeat
# (mm, NOT scaled by man height) + default texture + default local corridor
# subdivision level (see tree_pads.refine_and_displace_along_path — a
# textured feature needs denser mesh to resolve its texture, a plain SIMPLE
# carve doesn't). Unlike width, depth_mm/repeat_mm/local_subdiv are fixed
# values regardless of model scale.
_TYPE_DEFAULTS = {
    "SIMPLE": {"width_factor": 1.0, "depth_mm": 0.5, "repeat_mm": 10.0,
               "texture": "NONE", "local_subdiv": 0},
    "GRAVEL": {"width_factor": 0.8, "depth_mm": 0.5, "repeat_mm": 10.0,
               "texture": "BRICK_GRAVEL", "local_subdiv": 2},
    "PAVED_ROAD": {"width_factor": 1.0, "depth_mm": 1.0, "repeat_mm": 10.0,
                   "texture": "STONE_ROAD", "local_subdiv": 2},
}

# RIVER's field shape is different enough (levels instead of mm depth, an
# angle, a variation amount, a bottom-style enum, no texture/repeat) that it
# gets its own defaults dict rather than a squeezed-in _TYPE_DEFAULTS entry.
# width_factor and embankment_variation_factor are man_height_mm-scaled
# (model-scale-aware, resolved once here then freely hand-editable in mm,
# exactly like width_mm already is for the other three types); depth_levels
# and embankment_angle_deg are unit-less/degrees literals.
_RIVER_DEFAULTS = {
    "width_factor": 3.0,
    "depth_levels": 1,
    "local_subdiv": 3,
    "embankment_angle_deg": 45.0,
    "embankment_variation_factor": 0.5,
    "river_bottom_style": "NONE",
    "preserve_edge": True,
}


def apply_type_defaults(feature, man_height_mm):
    """Fill `feature`'s type-appropriate fields from `_TYPE_DEFAULTS`/
    `_RIVER_DEFAULTS`. width_mm (and, for RIVER, embankment_variation_mm) is
    a direct factor of man_height_mm (model-scale-aware); every other field
    is a fixed literal, not scaled by man height. Called directly (not just
    via the `feature_type` update callback) so a freshly drawn line is
    correctly sized even when its type equals the property's own default and
    no update fires."""
    if feature.feature_type == 'RIVER':
        d = _RIVER_DEFAULTS
        feature.width_mm = d["width_factor"] * man_height_mm
        feature.depth_levels = d["depth_levels"]
        feature.local_subdiv = d["local_subdiv"]
        feature.embankment_angle_deg = d["embankment_angle_deg"]
        feature.embankment_variation_mm = (
            d["embankment_variation_factor"] * man_height_mm)
        feature.river_bottom_style = d["river_bottom_style"]
        feature.preserve_edge = d["preserve_edge"]
        return
    d = _TYPE_DEFAULTS.get(feature.feature_type)
    if d is None:
        return
    feature.width_mm = d["width_factor"] * man_height_mm
    feature.depth_mm = d["depth_mm"]
    feature.repeat_mm = d["repeat_mm"]
    feature.texture = d["texture"]
    feature.local_subdiv = d["local_subdiv"]


# ---------------------------------------------------------------------------
# Texture asset cache — mirrors flora.py's _get_or_import_mesh pattern.

_HEIGHTMAP_CACHE = {}


def _get_or_load_heightmap(key):
    """(pixels, width, height) for PATH_TEXTURES[key], or None for "NONE"/a
    missing file. `pixels` is a flat row-major list of grayscale floats in
    [0, 1] (the R channel — a height PNG is authored as luminance),
    downsampled to HEIGHTMAP_WORKING_RES first. Session-cached."""
    if key in _HEIGHTMAP_CACHE:
        return _HEIGHTMAP_CACHE[key]

    entry = PATH_TEXTURES.get(key)
    filename = entry["file"] if entry else None
    if not filename:
        _HEIGHTMAP_CACHE[key] = None
        return None

    import os
    path = os.path.join(os.path.dirname(__file__), "assets", "Path Features", filename)
    if not os.path.isfile(path):
        _HEIGHTMAP_CACHE[key] = None
        return None

    image = bpy.data.images.load(path, check_existing=True)
    image.colorspace_settings.name = 'Non-Color'
    if image.size[0] > HEIGHTMAP_WORKING_RES or image.size[1] > HEIGHTMAP_WORKING_RES:
        image.scale(HEIGHTMAP_WORKING_RES, HEIGHTMAP_WORKING_RES)
    width, height = image.size[0], image.size[1]
    raw = image.pixels[:]
    channels = image.channels
    pixels = [raw[i * channels] for i in range(width * height)]
    result = (pixels, width, height)
    _HEIGHTMAP_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# River ripple synthesis — bakes one static snapshot of Blender's built-in
# Ocean modifier (a port of the Houdini Ocean Toolkit, itself an
# implementation of Tessendorf's Fourier-domain/Phillips-spectrum ocean-wave
# method) into a plain [0, 1] height grid, consumed exactly like a loaded
# PNG heightmap by tree_pads.sample_grayscale — no new bpy-free FFT/numpy
# math needed anywhere. This has to live here rather than in
# tree_pads.py/mesh_builder.py: bpy.types.OceanModifier is a real mesh
# modifier and can only be touched from the bpy-importing layer.

_OCEAN_HEIGHTFIELD_CACHE = {}
_OCEAN_SCRATCH_COLLECTION_NAME = "HexFinity Scratch"

# Fixed, not user-exposed — the user only asked for a Flat/Tessendorf's-FFT
# choice, not wave-tuning knobs. Choppiness is forced to 0 deliberately (see
# _generate_ocean_heightfield): any horizontal displacement would move a
# GENERATE-mode vertex off the regular base grid, breaking the "bucket by
# (x, y) position" grid-reconstruction below.
_OCEAN_RESOLUTION = 5
_OCEAN_WIND_VELOCITY = 5.0
_OCEAN_CHOPPINESS = 0.0
_OCEAN_WAVE_SCALE = 1.0
_OCEAN_WAVE_ALIGNMENT = 0.0
_OCEAN_DAMPING = 0.5
_OCEAN_DEPTH = 200.0


def _get_ocean_scratch_collection():
    """Get-or-create a plain scratch collection to briefly hold an Ocean-
    modifier bake object. The object is created, evaluated, and removed
    within a single synchronous call below — Blender only redraws the
    viewport/outliner between operator invocations, never mid-call — so it
    is never actually visible to the user and needs no hidden/excluded
    view-layer setup."""
    coll = bpy.data.collections.get(_OCEAN_SCRATCH_COLLECTION_NAME)
    if coll is None:
        coll = bpy.data.collections.new(_OCEAN_SCRATCH_COLLECTION_NAME)
    if coll.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(coll)
    return coll


def _generate_ocean_heightfield(seed, patch_mm, resolution=_OCEAN_RESOLUTION):
    """(pixels, width, height) or None — a static Tessendorf-FFT ocean-wave
    height snapshot baked from Blender's built-in Ocean modifier, normalized
    to [0, 1] (the same convention as a loaded grayscale heightmap PNG) and
    returned as a flat row-major Python list. Session-cached by
    (seed, resolution, patch_mm) — mirrors _get_or_load_heightmap.

    The Ocean modifier computes its height field as a pure function of
    `time` from the Phillips-spectrum synthesis, so no bake-to-disk
    (`ocean_bake`) is needed for a single static snapshot — baking only
    matters for accumulating *foam* over an animated frame range, which
    this doesn't use. `time` is fixed at 0.0; `random_seed` alone varies
    the result between rivers/tiles.

    Grid dimensions are discovered from the actual evaluated mesh (bucketed
    by vertex (x, y) position, not raw vertex index or an assumed formula
    from `resolution`) rather than assumed, since Blender's exact internal
    vertex ordering/resolution-to-grid-size mapping isn't a documented
    contract to rely on.
    """
    key = (seed, resolution, round(patch_mm, 3))
    if key in _OCEAN_HEIGHTFIELD_CACHE:
        return _OCEAN_HEIGHTFIELD_CACHE[key]

    coll = _get_ocean_scratch_collection()
    mesh = bpy.data.meshes.new("HF_OceanScratch")
    obj = bpy.data.objects.new("HF_OceanScratch", mesh)
    coll.objects.link(obj)
    obj.hide_render = True
    result = None
    try:
        mod = obj.modifiers.new("Ocean", 'OCEAN')
        mod.geometry_mode = 'GENERATE'
        mod.spatial_size = max(int(round(patch_mm)), 1)
        mod.resolution = resolution
        mod.random_seed = seed % 2147483647
        mod.time = 0.0
        mod.wind_velocity = _OCEAN_WIND_VELOCITY
        mod.choppiness = _OCEAN_CHOPPINESS
        mod.wave_scale = _OCEAN_WAVE_SCALE
        mod.wave_alignment = _OCEAN_WAVE_ALIGNMENT
        mod.damping = _OCEAN_DAMPING
        mod.depth = _OCEAN_DEPTH

        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        eval_mesh = eval_obj.to_mesh()
        try:
            coords = [(v.co.x, v.co.y, v.co.z) for v in eval_mesh.vertices]
        finally:
            eval_obj.to_mesh_clear()

        if coords:
            xs = sorted(set(round(c[0], 6) for c in coords))
            ys = sorted(set(round(c[1], 6) for c in coords))
            width, height = len(xs), len(ys)
            x_index = {x: i for i, x in enumerate(xs)}
            y_index = {y: i for i, y in enumerate(ys)}
            grid = [0.0] * (width * height)
            for (x, y, z) in coords:
                ix = x_index[round(x, 6)]
                iy = y_index[round(y, 6)]
                grid[iy * width + ix] = z
            z_min, z_max = min(grid), max(grid)
            span = z_max - z_min
            if span > 1e-9:
                pixels = [(z - z_min) / span for z in grid]
            else:
                pixels = [0.5] * len(grid)
            result = (pixels, width, height)
    finally:
        bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.meshes.remove(mesh)

    _OCEAN_HEIGHTFIELD_CACHE[key] = result
    return result


def _river_seed(tile_props, feature_index):
    """Deterministic per-river seed: the tile's own seed formula (mirrors
    operators.py's `surface_seed`) XORed with the feature's own index, so
    multiple rivers on one tile get decorrelated but reproducible seeds for
    both the embankment-variation noise and the ocean-ripple bake."""
    tile_seed = ((tile_props.coord_q * 73856093)
                 ^ (tile_props.coord_r * 19349663))
    return (tile_seed ^ (feature_index * 668265263)) & 0x7FFFFFFF


def path_specs(tile_obj):
    """Turn `tile_obj`'s path_features into mesh_builder.build_hex_tile's
    `path_features` kwarg list — mirrors flora.pad_specs(obj) /
    operators.terrain_pad_specs(obj). Each spec is tagged `"kind"`:
    `"texture"` (SIMPLE/GRAVEL/PAVED_ROAD, consumed by
    tree_pads.refine_and_displace_along_path) or `"river"` (RIVER, consumed
    by tree_pads.refine_and_carve_river) — mesh_builder.build_hex_tile
    partitions the list by this tag.

    A river's `depth_mm` is resolved fresh from `depth_levels *
    level_height_mm` on every call — unlike `width_mm` (resolved once at
    type-select time, then freely hand-editable in mm), a river's depth is
    always re-derived from the current scene-wide Level Height, the same
    way corner heights are never resolved-once either.

    `preserve_edge` (default True, mirrors the Terrain Brush's own
    Preserve Edge) is passed straight through: `tree_pads.
    refine_and_carve_river` damps the carve near the tile rim when it's on,
    or carves right up to the rim with a deterministic (unvaried)
    embankment when it's off, so a matching river drawn on the
    neighbouring tile can continue it.
    """
    tile = tile_obj.hexfinity_tile
    map_props = bpy.context.scene.hexfinity_map
    specs = []
    for i, feature in enumerate(tile.path_features):
        if len(feature.points) < 2:
            continue
        if feature.feature_type == 'RIVER':
            seed = _river_seed(tile, i)
            spec = {
                "kind": "river",
                "points": [(p.x, p.y) for p in feature.points],
                "width_mm": feature.width_mm,
                "depth_mm": feature.depth_levels * map_props.level_height_mm,
                "embankment_angle_deg": feature.embankment_angle_deg,
                "embankment_variation_mm": feature.embankment_variation_mm,
                "river_bottom_style": feature.river_bottom_style,
                "local_subdiv": feature.local_subdiv,
                "preserve_edge": feature.preserve_edge,
                "seed": seed,
            }
            if feature.river_bottom_style == 'TESSENDORF_FFT':
                patch_mm = max(feature.width_mm * 2.0, 50.0)
                heightfield = _generate_ocean_heightfield(seed, patch_mm)
                if heightfield is not None:
                    pixels, tex_width, tex_height = heightfield
                    spec["pixels"] = pixels
                    spec["tex_width"] = tex_width
                    spec["tex_height"] = tex_height
                    spec["ripple_patch_mm"] = patch_mm
            specs.append(spec)
            continue
        heightmap = _get_or_load_heightmap(feature.texture)
        pixels, tex_width, tex_height = heightmap if heightmap else (None, 0, 0)
        specs.append({
            "kind": "texture",
            "points": [(p.x, p.y) for p in feature.points],
            "width_mm": feature.width_mm,
            "depth_mm": feature.depth_mm,
            "blend_mm": max(feature.width_mm * 0.15, 1.0),
            "repeat_mm": max(feature.repeat_mm, 1.0),
            "pixels": pixels,
            "tex_width": tex_width,
            "tex_height": tex_height,
            "local_subdiv": feature.local_subdiv,
        })
    return specs


# ---------------------------------------------------------------------------
# Drawing modal operator + list UI.

def _feature_plane_z_local(tile, map_props):
    """Tile-local z of the "man height above the hex" drawing plane: the
    tile's tallest corner, plus one man-height of clearance."""
    levels = (tile.p1, tile.p2, tile.p3, tile.p4, tile.p5, tile.p6)
    return (map_props.base_thickness_mm
            + max(0, max(levels)) * map_props.level_height_mm
            + map_props.man_height_mm)


def _mouse_on_plane(context, event, z_world):
    """Intersect the mouse ray with the horizontal world plane z=z_world.

    Returns a Vector, or None for degenerate side-on views where the ray is
    (near-)parallel to the plane. Small local copy of gizmo._mouse_on_plane
    — the codebase's convention (see brush.py/flora.py) is to copy-adapt
    this kind of small raycast helper per file rather than reach into
    another module's leading-underscore helper."""
    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return None
    coord = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    if abs(direction.z) < 1e-6:
        return None
    t = (z_world - origin.z) / direction.z
    return origin + direction * t


def _snap_targets_world(obj, map_props, edge_snap):
    """[(Vector, edge_idx_or_None), ...] of every valid snap target for a
    line drawn on `obj`: this tile's hex-edge snap points (tagged with which
    of the 6 edges they belong to, per edge_snap_points' documented
    edge-by-edge ordering — `path_features._resolve_crossing_neighbour` uses
    this to know which neighbour a crossing click borders) plus every
    waypoint of its already-committed path features (tagged None — a same-
    tile join, never a boundary crossing). The in-progress line isn't in
    this list yet, so nothing extra needs excluding."""
    tile = obj.hexfinity_tile
    z_local = _feature_plane_z_local(tile, map_props)
    mw = obj.matrix_world
    n = max(2, edge_snap)
    per_edge = n - 1
    targets = []
    for i, (x, y) in enumerate(edge_snap_points(map_props.diameter_mm, edge_snap)):
        targets.append((mw @ Vector((x, y, z_local)), i // per_edge))
    for feature in tile.path_features:
        for p in feature.points:
            targets.append((mw @ Vector((p.x, p.y, z_local)), None))
    return targets


def _commit_feature(context, obj, pts_local, feature_type='SIMPLE', seed_settings=None):
    """Append a feature with `pts_local` (list of (x, y) tile-local mm) to
    `obj` and make it active. Setting feature_type fires the property
    callback that auto-fills width/depth/repeat/texture + a default name
    and rebuilds the tile — the points are already in place so the carve
    renders correctly (mirrors regions._commit_region).

    `seed_settings`, when given, is a {field: value} dict over
    `_PATH_FEATURE_LINK_FIELDS` (the just-crossed-from segment's own
    settings) — used instead of the plain `feature_type` default when a
    multi-hex draw continues onto this tile, so the new segment reads as a
    continuation of the same road/path/river rather than a fresh SIMPLE
    line. Applied with the same guarded-overwrite shape
    `HEXFINITY_OT_link_connected_paths.execute()` uses: feature_type is set
    first (unguarded, since its own update callback always fires a
    transient type-defaulted rebuild), then every other field is written
    under `_PATH_FEATURE_FILLING` so those type defaults get overwritten by
    the actually-inherited values before the final rebuild."""
    tile = obj.hexfinity_tile
    feature = tile.path_features.add()
    for (x, y) in pts_local:
        p = feature.points.add()
        p.x, p.y = x, y
    tile.active_path_feature_index = len(tile.path_features) - 1
    feature.feature_type = seed_settings["feature_type"] if seed_settings else feature_type
    if seed_settings:
        from . import properties
        from . import operators
        properties._PATH_FEATURE_FILLING = True
        try:
            for f in _PATH_FEATURE_LINK_FIELDS:
                if f == "feature_type":
                    continue
                setattr(feature, f, seed_settings[f])
        finally:
            properties._PATH_FEATURE_FILLING = False
        # The guarded overwrites above deliberately don't trigger their own
        # per-field rebuild (that's the point of the guard) -- the
        # feature_type assignment already rebuilt once with type-defaulted
        # values, so rebuild once more now the actually-inherited values are
        # in place. Same two-rebuilds-per-write shape as
        # HEXFINITY_OT_link_connected_paths.execute()/apply_surface_texture.
        operators.rebuild_tile(obj)


class HEXFINITY_OT_draw_path_feature(bpy.types.Operator):
    bl_idname = "hexfinity.draw_path_feature"
    bl_label = "Draw Path Feature"
    bl_description = ("Click points above the active tile to draw a line. "
                      "Clicking near another line's waypoint snaps to it and "
                      "ends the line. Clicking near a hex edge point ends the "
                      "line there, or continues it onto the neighbouring hex "
                      "if that hex is also selected. Enter/RMB finishes "
                      "early, Backspace removes the last point, Esc cancels.")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.scene.hexfinity_map.is_generated
                and obj is not None and obj.hexfinity_tile.is_generated)

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Draw Feature must be started in the 3D viewport")
            return {'CANCELLED'}
        self._tile = context.active_object
        self._pts_local = []     # [(x, y)] tile-local mm — committed to the line
        self._pts_world = []     # [Vector] world positions for drawing the line
        self._cursor = (event.mouse_region_x, event.mouse_region_y)
        self._snap_hint = None   # (world Vector, edge_idx_or_None) of the hovered snap target
        self._pending_seed_settings = None  # settings to inherit on this tile's next commit
        self._pan_timer = None       # wm timer, only while a pan animation is running
        self._pan_start = None       # Vector: view_location when the current pan began
        self._pan_target = None      # Vector: view_location the current pan is heading to
        self._pan_start_time = 0.0   # time.monotonic() when the current pan began
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Draw Path Feature:  LMB = add point    "
            "snap to line = finish    snap to selected-neighbour edge = "
            "continue there    Enter/RMB = finish (2+ pts)    "
            "Backspace = undo point    Esc = cancel")
        self._update_snap_hint(context)
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cursor = (event.mouse_region_x, event.mouse_region_y)
            self._update_snap_hint(context)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if context.region is None or context.region.type != 'WINDOW':
                return {'PASS_THROUGH'}
            result = self._add_point(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return result if result is not None else {'RUNNING_MODAL'}

        if event.type in {'BACK_SPACE', 'DEL'} and event.value == 'PRESS':
            if self._pts_local:
                self._pts_local.pop()
                self._pts_world.pop()
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if (event.type in {'RET', 'NUMPAD_ENTER', 'RIGHTMOUSE'}
                and event.value == 'PRESS'):
            return self._close(context)

        if event.type == 'ESC' and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            self._advance_view_pan(context)
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}

    def _find_snap_target(self, context, coord):
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return None
        map_props = context.scene.hexfinity_map
        edge_snap = context.scene.hexfinity_path_features.edge_snap
        best = None
        best_dist = SNAP_RADIUS_PX
        for w, edge_idx in _snap_targets_world(self._tile, map_props, edge_snap):
            s = view3d_utils.location_3d_to_region_2d(region, rv3d, w)
            if s is None:
                continue
            dist = math.hypot(s.x - coord[0], s.y - coord[1])
            if dist <= best_dist:
                best_dist = dist
                best = (w, edge_idx)
        return best

    def _update_snap_hint(self, context):
        self._snap_hint = self._find_snap_target(context, self._cursor)

    def _add_point(self, context, event):
        coord = (event.mouse_region_x, event.mouse_region_y)
        hit = self._find_snap_target(context, coord)
        if hit is not None:
            target, edge_idx = hit
            lp = self._tile.matrix_world.inverted() @ target
            was_empty = not self._pts_local
            self._pts_local.append((lp.x, lp.y))
            self._pts_world.append(target.copy())
            if was_empty:
                return None
            return self._close(context, crossing_edge_idx=edge_idx,
                               crossing_point_world=target)

        map_props = context.scene.hexfinity_map
        z_local = _feature_plane_z_local(self._tile.hexfinity_tile, map_props)
        z_world = (self._tile.matrix_world @ Vector((0.0, 0.0, z_local))).z
        hit = _mouse_on_plane(context, event, z_world)
        if hit is None:
            self.report({'INFO'}, "Can't place a point from this viewing angle")
            return None
        lp = self._tile.matrix_world.inverted() @ hit
        if not point_in_hex(lp.x, lp.y, map_props.diameter_mm):
            self.report({'INFO'}, "Point must be inside the selected hex")
            return None
        self._pts_local.append((lp.x, lp.y))
        self._pts_world.append(hit.copy())
        return None

    def _close(self, context, crossing_edge_idx=None, crossing_point_world=None):
        if len(self._pts_local) < 2:
            self.report({'WARNING'}, "A line needs at least 2 points")
            return {'RUNNING_MODAL'}
        _commit_feature(context, self._tile, self._pts_local,
                        seed_settings=self._pending_seed_settings)
        bpy.ops.ed.undo_push(message="HexFinity Draw Path Feature")

        neighbour = self._resolve_crossing_neighbour(context, crossing_edge_idx)
        if neighbour is not None:
            self._continue_onto(context, neighbour, crossing_point_world)
            return {'RUNNING_MODAL'}

        self._finish(context)
        return {'FINISHED'}

    def _resolve_crossing_neighbour(self, context, edge_idx):
        """The neighbour tile across `self._tile`'s edge `edge_idx`, or None
        if there's nothing to continue onto: `edge_idx` is None (the snap was
        an existing waypoint, not a hex-edge point), there's no generated
        tile there (a real map edge), or that tile isn't currently selected
        — selection is the sole gate on whether an edge-point click
        continues the line or ends it, per the tool's multi-hex workflow:
        select every hex a path should span before drawing."""
        if edge_idx is None:
            return None
        tile_props = self._tile.hexfinity_tile
        direction = EDGE_DIRECTIONS[edge_idx]
        nq, nr = neighbour_coord(tile_props.coord_q, tile_props.coord_r, direction)
        neighbour = find_tile(context.scene, nq, nr)
        if neighbour is None or neighbour not in context.selected_objects:
            return None
        return neighbour

    def _continue_onto(self, context, neighbour, world_point):
        """Start a new segment on `neighbour`, seeded with the shared
        `world_point` and the just-committed segment's settings, and begin a
        smooth viewport pan onto it."""
        prev_tile = self._tile.hexfinity_tile
        prev_feature = prev_tile.path_features[prev_tile.active_path_feature_index]
        self._pending_seed_settings = {
            f: getattr(prev_feature, f) for f in _PATH_FEATURE_LINK_FIELDS}

        self._tile = neighbour
        lp = neighbour.matrix_world.inverted() @ world_point
        self._pts_local = [(lp.x, lp.y)]
        self._pts_world = [world_point.copy()]
        context.view_layer.objects.active = neighbour
        self._start_view_pan(context, neighbour)

    def _start_view_pan(self, context, tile_obj):
        """Begin (or redirect, if already panning) a smooth glide of the
        viewport to `tile_obj`'s centre over VIEW_PAN_DURATION_S, preserving
        rotation/distance/perspective, so a multi-hex draw keeps the same
        viewing angle as it crosses each boundary. Driven by a wm timer
        ticked via modal()'s TIMER branch rather than blocking -- point
        placement keeps working during the glide, it just samples whatever
        camera position the current tick left. Crossing a second boundary
        before the first glide finishes simply redirects it: the *current*
        (mid-flight) view_location becomes the new start, and the existing
        timer is reused rather than adding a second one."""
        rv3d = context.region_data
        if rv3d is None:
            return
        self._pan_start = rv3d.view_location.copy()
        self._pan_target = tile_obj.matrix_world.translation.copy()
        self._pan_start_time = time.monotonic()
        if self._pan_timer is None:
            self._pan_timer = context.window_manager.event_timer_add(
                1.0 / 60.0, window=context.window)

    def _advance_view_pan(self, context):
        if self._pan_start is None:
            return
        rv3d = context.region_data
        if rv3d is None:
            return
        t = min(1.0, (time.monotonic() - self._pan_start_time) / VIEW_PAN_DURATION_S)
        eased = t * t * (3.0 - 2.0 * t)  # smoothstep ease-in-out
        rv3d.view_location = self._pan_start.lerp(self._pan_target, eased)
        if context.area is not None:
            context.area.tag_redraw()
        if t >= 1.0:
            self._stop_view_pan(context)

    def _stop_view_pan(self, context):
        if self._pan_timer is not None:
            context.window_manager.event_timer_remove(self._pan_timer)
            self._pan_timer = None
        self._pan_start = None
        self._pan_target = None

    def _finish(self, context):
        self._stop_view_pan(context)
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)
        if context.area is not None:
            context.area.tag_redraw()

    def _draw(self, context):
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return
        pts2d = []
        for w in self._pts_world:
            p = view3d_utils.location_3d_to_region_2d(region, rv3d, w)
            if p is not None:
                pts2d.append((p.x, p.y))

        tip = self._cursor
        tip_color = _LINE_COLOR
        if self._snap_hint is not None:
            s = view3d_utils.location_3d_to_region_2d(region, rv3d, self._snap_hint[0])
            if s is not None:
                tip = (s.x, s.y)
                tip_color = _SNAP_COLOR

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        shader.bind()

        # Rubber-band from the last committed point to the cursor/snap tip.
        preview = list(pts2d) + [tip]
        shader.uniform_float("color", _LINE_COLOR)
        batch_for_shader(shader, 'LINE_STRIP', {"pos": preview}).draw(shader)

        # The tip dot (highlighted when it would snap) plus committed dots.
        gpu.state.point_size_set(7.0)
        shader.uniform_float("color", tip_color)
        batch_for_shader(shader, 'POINTS', {"pos": [tip]}).draw(shader)
        if pts2d:
            shader.uniform_float("color", _POINT_COLOR)
            batch_for_shader(shader, 'POINTS', {"pos": pts2d}).draw(shader)

        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')


class HEXFINITY_OT_remove_path_feature(bpy.types.Operator):
    bl_idname = "hexfinity.remove_path_feature"
    bl_label = "Remove Path Feature"
    bl_description = "Remove the active path feature line from this tile"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.hexfinity_tile.is_generated
                and len(obj.hexfinity_tile.path_features) > 0)

    def execute(self, context):
        obj = context.active_object
        tile = obj.hexfinity_tile
        idx = tile.active_path_feature_index
        if 0 <= idx < len(tile.path_features):
            feature = tile.path_features[idx]
            if feature.feature_type == 'SEGMENT' and feature.segment_piece is not None:
                # Unlike a carved groove (which disappears on the next
                # rebuild once its points are gone), a placed segment is a
                # real parented Object — removing the list entry alone
                # would leave it orphaned in the scene.
                piece = feature.segment_piece
                mesh = piece.data
                bpy.data.objects.remove(piece, do_unlink=True)
                if mesh is not None and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            tile.path_features.remove(idx)
            tile.active_path_feature_index = min(idx, len(tile.path_features) - 1)
            from . import operators
            operators.rebuild_tile(obj)
        return {'FINISHED'}


def _world_points(obj, feature):
    """A path feature's waypoints in world XY (tiles are translation-only
    placed, matching operators._tile_under_point's tile.location.x/y use)."""
    return [(obj.location.x + p.x, obj.location.y + p.y) for p in feature.points]


def _all_path_nodes(scene):
    """({(q, r, feature_index): [world (x,y), ...]}, {(q, r): tile_obj}) for
    every path feature on every generated tile in the map. Mirrors the
    guard/iteration shape of operators.on_global_update / _corner_lookup."""
    map_props = scene.hexfinity_map
    coll = map_props.root_collection
    nodes, tiles = {}, {}
    if not map_props.is_generated or coll is None:
        return nodes, tiles
    for obj in coll.objects:
        tp = obj.hexfinity_tile
        if not tp.is_generated:
            continue
        tiles[(tp.coord_q, tp.coord_r)] = obj
        for i, feature in enumerate(tp.path_features):
            nodes[(tp.coord_q, tp.coord_r, i)] = _world_points(obj, feature)
    return nodes, tiles


class HEXFINITY_OT_link_connected_paths(bpy.types.Operator):
    bl_idname = "hexfinity.link_connected_paths"
    bl_label = "Link Connected Paths"
    bl_description = ("Apply this path's settings (not its waypoints) to "
                      "every other path feature transitively connected to "
                      "it via a shared waypoint, anywhere in the map")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or not obj.hexfinity_tile.is_generated:
            return False
        tile = obj.hexfinity_tile
        return 0 <= tile.active_path_feature_index < len(tile.path_features)

    def execute(self, context):
        scene = context.scene
        src_obj = context.active_object
        src_tile = src_obj.hexfinity_tile
        src_idx = src_tile.active_path_feature_index
        src_key = (src_tile.coord_q, src_tile.coord_r, src_idx)

        nodes, tile_lookup = _all_path_nodes(scene)
        if not nodes.get(src_key):
            self.report({'WARNING'}, "HexFinity: active path has no waypoints.")
            return {'CANCELLED'}

        connected = find_connected_component(nodes, src_key, _LINK_EPSILON_MM)
        connected.discard(src_key)
        if not connected:
            self.report({'WARNING'}, "HexFinity: no connected paths found.")
            return {'CANCELLED'}

        source_feature = src_tile.path_features[src_idx]
        clip = {f: getattr(source_feature, f) for f in _PATH_FEATURE_LINK_FIELDS}

        touched = {}
        for (q, r, i) in connected:
            touched.setdefault((q, r), []).append(i)

        from . import properties
        from . import operators

        for (q, r), indices in touched.items():
            obj = tile_lookup[(q, r)]
            tile = obj.hexfinity_tile
            for i in indices:
                feature = tile.path_features[i]
                # feature_type first, unguarded: its own update callback
                # always fires a transient rebuild with type-defaulted
                # values, which the guarded loop below then overwrites with
                # the actually-copied ones -- same two-rebuilds-per-write
                # shape as HEXFINITY_OT_apply_surface_texture.
                feature.feature_type = clip["feature_type"]
                properties._PATH_FEATURE_FILLING = True
                try:
                    for f in _PATH_FEATURE_LINK_FIELDS:
                        if f == "feature_type":
                            continue
                        setattr(feature, f, clip[f])
                finally:
                    properties._PATH_FEATURE_FILLING = False
            operators.rebuild_tile(obj)

        self.report({'INFO'},
                   f"Linked settings to {len(connected)} connected path "
                   f"feature(s) across {len(touched)} tile(s).")
        return {'FINISHED'}


class HEXFINITY_UL_path_features(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        npts = len(item.points)
        type_label = item.feature_type.replace('_', ' ').title()
        name = item.name or type_label
        layout.label(text=f"{name}  ({type_label}, {npts} pts)", icon='MOD_CURVE')
