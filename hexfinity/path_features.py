"""Path feature (footpath/track/road) authoring — draw an open waypoint
line above a tile, then auto-carve it into the tile's top surface.

A feature is an open polyline (tile-local mm) stored in the tile's
`path_features` CollectionProperty, plus a type. This module contains both
the bpy authoring UI — a modal waypoint picker (mirrors `regions.py`'s
point picker, but places points on a flat "man height" plane above the tile
instead of raycasting onto the mesh, and terminates the line automatically
when a click snaps to the tile's hex-edge points or to another already-drawn
line's waypoint) plus a remove operator and the list UI — and the bpy
texture-asset pipeline (`PATH_TEXTURES`/`_get_or_load_heightmap`) that turns
a line into `mesh_builder.build_hex_tile`'s `path_features` kwarg via
`path_specs()`. The actual curvilinear-sampling math is bpy-free, in
`tree_pads.refine_and_displace_along_path`.

Every edit (drawing a line, changing its type/width/depth/repeat/texture,
removing it) auto-rebuilds the tile — there is no manual "Generate" step,
matching every other generative feature in the codebase.

RIVER is a fourth feature_type with its own field shape (depth in map
Levels, an embankment angle + variation, a Flat/Tessendorf's-FFT bottom
style, no texture/repeat) and its own carve math
(tree_pads.refine_and_carve_river) — see _RIVER_DEFAULTS/path_specs()
below. Like every other pad/path/brush displacement in this codebase, a
river's depth fades to exactly 0 at the tile's rim edge (the invariant that
keeps two independently-built neighbouring tiles' shared edges matching).
For a river meant to continue into a neighbouring tile, lower the shared
corner Level(s) at the crossing edge to at least the river's own
depth_levels on both tiles (the per-corner Level sliders already
auto-propagate to the shared neighbour corner) — this makes the ambient
terrain the rim-fade reverts to already sit at the river's bed depth, so
the seam reads as continuous with no special-case code required.
"""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

from .map import edge_snap_points, point_in_hex


SNAP_RADIUS_PX = 18.0
_LINE_COLOR = (0.85, 0.55, 0.25, 0.9)
_SNAP_COLOR = (0.3, 1.0, 0.5, 0.95)
_POINT_COLOR = (1.0, 1.0, 1.0, 1.0)

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
    """World positions of every valid snap target for a line drawn on `obj`:
    this tile's hex-edge snap points, plus every waypoint of its already-
    committed path features (the in-progress line isn't in this list yet,
    so nothing extra needs excluding)."""
    tile = obj.hexfinity_tile
    z_local = _feature_plane_z_local(tile, map_props)
    mw = obj.matrix_world
    pts = [mw @ Vector((x, y, z_local))
           for (x, y) in edge_snap_points(map_props.diameter_mm, edge_snap)]
    for feature in tile.path_features:
        for p in feature.points:
            pts.append(mw @ Vector((p.x, p.y, z_local)))
    return pts


def _commit_feature(context, obj, pts_local, feature_type='SIMPLE'):
    """Append a feature with `pts_local` (list of (x, y) tile-local mm) to
    `obj` and make it active. Setting feature_type fires the property
    callback that auto-fills width/depth/repeat/texture + a default name
    and rebuilds the tile — the points are already in place so the carve
    renders correctly (mirrors regions._commit_region)."""
    tile = obj.hexfinity_tile
    feature = tile.path_features.add()
    for (x, y) in pts_local:
        p = feature.points.add()
        p.x, p.y = x, y
    tile.active_path_feature_index = len(tile.path_features) - 1
    feature.feature_type = feature_type


class HEXFINITY_OT_draw_path_feature(bpy.types.Operator):
    bl_idname = "hexfinity.draw_path_feature"
    bl_label = "Draw Path Feature"
    bl_description = ("Click points above the active tile to draw a line. "
                      "Clicking near the hex edge or another line's waypoint "
                      "snaps to it and ends the line. Enter/RMB finishes "
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
        self._snap_hint = None   # world Vector of the currently-hovered snap target
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Draw Path Feature:  LMB = add point    "
            "snap to edge/line = finish    Enter/RMB = finish (2+ pts)    "
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
        for w in _snap_targets_world(self._tile, map_props, edge_snap):
            s = view3d_utils.location_3d_to_region_2d(region, rv3d, w)
            if s is None:
                continue
            dist = math.hypot(s.x - coord[0], s.y - coord[1])
            if dist <= best_dist:
                best_dist = dist
                best = w
        return best

    def _update_snap_hint(self, context):
        self._snap_hint = self._find_snap_target(context, self._cursor)

    def _add_point(self, context, event):
        coord = (event.mouse_region_x, event.mouse_region_y)
        target = self._find_snap_target(context, coord)
        if target is not None:
            lp = self._tile.matrix_world.inverted() @ target
            was_empty = not self._pts_local
            self._pts_local.append((lp.x, lp.y))
            self._pts_world.append(target.copy())
            if was_empty:
                return None
            return self._close(context)

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

    def _close(self, context):
        if len(self._pts_local) < 2:
            self.report({'WARNING'}, "A line needs at least 2 points")
            return {'RUNNING_MODAL'}
        _commit_feature(context, self._tile, self._pts_local)
        bpy.ops.ed.undo_push(message="HexFinity Draw Path Feature")
        self._finish(context)
        return {'FINISHED'}

    def _finish(self, context):
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
            s = view3d_utils.location_3d_to_region_2d(region, rv3d, self._snap_hint)
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
            tile.path_features.remove(idx)
            tile.active_path_feature_index = min(idx, len(tile.path_features) - 1)
            from . import operators
            operators.rebuild_tile(obj)
        return {'FINISHED'}


class HEXFINITY_UL_path_features(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        npts = len(item.points)
        type_label = item.feature_type.replace('_', ' ').title()
        name = item.name or type_label
        layout.label(text=f"{name}  ({type_label}, {npts} pts)", icon='MOD_CURVE')
