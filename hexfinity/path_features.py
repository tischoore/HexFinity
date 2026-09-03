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


def apply_type_defaults(feature, man_height_mm):
    """Fill `feature`'s width_mm/depth_mm/repeat_mm/texture/local_subdiv
    from `_TYPE_DEFAULTS[feature.feature_type]`. width_mm is a direct factor
    of man_height_mm (model-scale-aware); depth_mm/repeat_mm/local_subdiv
    are fixed literal values, not scaled by man height. Called directly (not
    just via the `feature_type` update callback) so a freshly drawn line is
    correctly sized even when its type equals the property's own default and
    no update fires."""
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


def path_specs(tile_obj):
    """Turn `tile_obj`'s path_features into mesh_builder.build_hex_tile's
    `path_features` kwarg list — mirrors flora.pad_specs(obj) /
    operators.terrain_pad_specs(obj)."""
    tile = tile_obj.hexfinity_tile
    specs = []
    for feature in tile.path_features:
        if len(feature.points) < 2:
            continue
        heightmap = _get_or_load_heightmap(feature.texture)
        pixels, tex_width, tex_height = heightmap if heightmap else (None, 0, 0)
        specs.append({
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
