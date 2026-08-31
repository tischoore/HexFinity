"""Terrain feature (footpath/track/road) authoring — draw an open waypoint
line above a tile.

A feature is an open polyline (tile-local mm) stored in the tile's
`terrain_features` CollectionProperty, plus a type. This module only
contains the bpy authoring UI: a modal waypoint picker (mirrors
`regions.py`'s point picker, but places points on a flat "man height" plane
above the tile instead of raycasting onto the mesh, and terminates the line
automatically when a click snaps to the tile's hex-edge points or to another
already-drawn line's waypoint) plus small remove/generate operators and the
list UI.

Turning drawn features into mesh geometry (the "Generate" button) is future
work — `HEXFINITY_OT_generate_terrain_features` is a permanently-disabled
placeholder for now.
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
    committed terrain features (the in-progress line isn't in this list
    yet, so nothing extra needs excluding)."""
    tile = obj.hexfinity_tile
    z_local = _feature_plane_z_local(tile, map_props)
    mw = obj.matrix_world
    pts = [mw @ Vector((x, y, z_local))
           for (x, y) in edge_snap_points(map_props.diameter_mm, edge_snap)]
    for feature in tile.terrain_features:
        for p in feature.points:
            pts.append(mw @ Vector((p.x, p.y, z_local)))
    return pts


def _commit_feature(obj, pts_local):
    """Append a feature with `pts_local` (list of (x, y) tile-local mm) to
    `obj` and make it active."""
    tile = obj.hexfinity_tile
    feature = tile.terrain_features.add()
    for (x, y) in pts_local:
        p = feature.points.add()
        p.x, p.y = x, y
    feature.name = f"Feature {len(tile.terrain_features)}"
    tile.active_terrain_feature_index = len(tile.terrain_features) - 1


class HEXFINITY_OT_draw_terrain_feature(bpy.types.Operator):
    bl_idname = "hexfinity.draw_terrain_feature"
    bl_label = "Draw Terrain Feature"
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
            "Draw Terrain Feature:  LMB = add point    "
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
        edge_snap = context.scene.hexfinity_terrain_features.edge_snap
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
        _commit_feature(self._tile, self._pts_local)
        bpy.ops.ed.undo_push(message="HexFinity Draw Terrain Feature")
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


class HEXFINITY_OT_remove_terrain_feature(bpy.types.Operator):
    bl_idname = "hexfinity.remove_terrain_feature"
    bl_label = "Remove Terrain Feature"
    bl_description = "Remove the active terrain feature line from this tile"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.hexfinity_tile.is_generated
                and len(obj.hexfinity_tile.terrain_features) > 0)

    def execute(self, context):
        obj = context.active_object
        tile = obj.hexfinity_tile
        idx = tile.active_terrain_feature_index
        if 0 <= idx < len(tile.terrain_features):
            tile.terrain_features.remove(idx)
            tile.active_terrain_feature_index = min(idx, len(tile.terrain_features) - 1)
        return {'FINISHED'}


class HEXFINITY_OT_generate_terrain_features(bpy.types.Operator):
    bl_idname = "hexfinity.generate_terrain_features"
    bl_label = "Generate"
    bl_description = ("Not yet implemented — will build 3D path geometry from "
                      "drawn terrain features in a future update")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return False

    def execute(self, context):
        return {'FINISHED'}


class HEXFINITY_UL_terrain_features(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        npts = len(item.points)
        type_label = item.feature_type.replace('_', ' ').title()
        name = item.name or type_label
        layout.label(text=f"{name}  ({type_label}, {npts} pts)", icon='MOD_CURVE')
