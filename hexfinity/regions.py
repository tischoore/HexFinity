"""Procedural-surface region authoring — draw a closed loop on a tile top.

A region is a closed polygon (tile-local mm) stored in the tile's
`surface_regions` CollectionProperty, plus a surface type, params, and a main
direction. The bpy-free masking/displacement lives in `procedural_surfaces` +
`mesh_builder`; this module only contains the bpy authoring UI: a modal point
picker (mirrors `brush.py` — raycast clicks onto the tile, draw the in-progress
loop) and small add/remove operators + the list UI.

Like everything in HexFinity the result is data: the region is re-evaluated by
`operators.rebuild_tile` → `build_hex_tile` on every rebuild, so it survives
height edits and subdivision changes (the polygon is in continuous XY).
"""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

from . import face_select


_DEFAULT_SURFACE = "COBBLESTONE"


def _commit_region(obj, pts_local):
    """Append a region with `pts_local` (list of (x, y) tile-local mm) to `obj`
    and make it active. Setting the surface type fires the property callback
    that auto-fills params and rebuilds the tile."""
    tile = obj.hexfinity_tile
    region = tile.surface_regions.add()
    for (x, y) in pts_local:
        p = region.points.add()
        p.x, p.y = x, y
    tile.active_surface_region_index = len(tile.surface_regions) - 1
    # Triggers _on_region_type_update -> auto-fill feature/depth/regularity +
    # rebuild_tile (the points are already in place so the texture renders).
    region.surface_type = _DEFAULT_SURFACE


class HEXFINITY_OT_draw_region(bpy.types.Operator):
    bl_idname = "hexfinity.draw_region"
    bl_label = "Draw Surface Region"
    bl_description = ("Click points on the active tile to outline a region, then "
                      "Enter/RMB to close it. Backspace removes the last point, "
                      "Esc cancels.")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.scene.hexfinity_map.is_generated
                and obj is not None and obj.hexfinity_tile.is_generated)

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Draw Region must be started in the 3D viewport")
            return {'CANCELLED'}
        self._tile = context.active_object
        self._pts_local = []     # [(x, y)] tile-local mm — committed to the region
        self._pts_world = []     # [Vector] world positions for drawing the loop
        self._cursor = (event.mouse_region_x, event.mouse_region_y)
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Draw Region:  LMB = add point    Enter/RMB = close    "
            "Backspace = undo point    Esc = cancel")
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cursor = (event.mouse_region_x, event.mouse_region_y)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if context.region is None or context.region.type != 'WINDOW':
                return {'PASS_THROUGH'}
            self._add_point(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

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

    def _add_point(self, context, event):
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, location, _n, _i, hit_obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction)
        if not hit or hit_obj is None or hit_obj.original != self._tile:
            self.report({'INFO'}, "Click on the active tile's surface")
            return
        lp = self._tile.matrix_world.inverted() @ location
        self._pts_local.append((lp.x, lp.y))
        self._pts_world.append(location.copy())

    def _close(self, context):
        if len(self._pts_local) < 3:
            self.report({'WARNING'}, "A region needs at least 3 points")
            return {'RUNNING_MODAL'}
        _commit_region(self._tile, self._pts_local)
        bpy.ops.ed.undo_push(message="HexFinity Draw Region")
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

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        shader.bind()

        # Rubber-band from the last point to the cursor, plus the closing edge.
        if pts2d:
            preview = list(pts2d) + [self._cursor]
            if len(pts2d) >= 2:
                preview.append(pts2d[0])
            shader.uniform_float("color", (1.0, 0.8, 0.2, 0.9))
            batch_for_shader(shader, 'LINE_STRIP', {"pos": preview}).draw(shader)

        # Vertex dots.
        if pts2d:
            gpu.state.point_size_set(7.0)
            shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
            batch_for_shader(shader, 'POINTS', {"pos": pts2d}).draw(shader)

        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')


class HEXFINITY_OT_flood_fill_region(bpy.types.Operator):
    """Magic-wand-style region authoring: hover the tile top to preview a
    connected patch of similarly-oriented faces (grown by `face_select`
    from the hovered seed face, angle-thresholded against
    `scene.hexfinity_flood_fill.angle_threshold_deg`), click to commit it.

    Flood fill is only ever an *input method* here — the committed result
    is the patch's boundary loop converted to a tile-local-XY polygon and
    handed to the same `_commit_region()` every hand-drawn region uses, so
    once committed a flood-filled region is indistinguishable from one
    drawn point-by-point (see `face_select.py`'s module docstring for why
    mesh face indices themselves are never stored)."""

    bl_idname = "hexfinity.flood_fill_region"
    bl_label = "Flood Fill Region"
    bl_description = ("Hover the active tile to preview a connected patch of "
                      "similarly-angled faces, LMB to commit it as a region. "
                      "Esc/RMB cancels.")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.scene.hexfinity_map.is_generated
                and obj is not None and obj.hexfinity_tile.is_generated)

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Flood Fill must be started in the 3D viewport")
            return {'CANCELLED'}
        self._tile = context.active_object
        self._mesh_cache_obj = None
        self._mesh_verts = None
        self._mesh_faces = None
        self._last_seed_index = None
        self._hover_selected = None   # set of face indices, or None if not hovering the tile
        self._hover_tris_world = []   # flattened world-space triangle verts for the preview
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_preview, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Flood Fill:  hover to preview    LMB = commit    Esc = cancel")
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._update_hover(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if context.region is None or context.region.type != 'WINDOW':
                return {'PASS_THROUGH'}
            if self._commit(context):
                return {'FINISHED'}
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _update_hover(self, context, event):
        region = context.region
        rv3d = context.region_data
        if region is None or region.type != 'WINDOW' or rv3d is None:
            return
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, _loc, _n, index, hit_obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction)
        if not hit or hit_obj is None or hit_obj.original != self._tile:
            self._hover_selected = None
            self._hover_tris_world = []
            self._last_seed_index = None
            return

        # Re-extract the mesh only when we start/resume hovering this tile —
        # not on every pixel of mouse motion.
        if self._mesh_cache_obj != hit_obj.original:
            mesh = hit_obj.data
            self._mesh_verts = [tuple(v.co) for v in mesh.vertices]
            self._mesh_faces = [tuple(p.vertices) for p in mesh.polygons]
            self._mesh_cache_obj = hit_obj.original
            self._last_seed_index = None

        if index == self._last_seed_index and self._hover_selected is not None:
            return
        self._last_seed_index = index

        angle_deg = context.scene.hexfinity_flood_fill.angle_threshold_deg
        self._hover_selected = face_select.flood_fill_faces(
            self._mesh_verts, self._mesh_faces, index, angle_deg)
        self._hover_tris_world = self._build_preview_tris()

    def _build_preview_tris(self):
        if not self._hover_selected:
            return []
        mw = self._tile.matrix_world
        tris = []
        for i in self._hover_selected:
            face = self._mesh_faces[i]
            n = len(face)
            v0 = self._mesh_verts[face[0]]
            for k in range(1, n - 1):
                v1 = self._mesh_verts[face[k]]
                v2 = self._mesh_verts[face[k + 1]]
                tris.append(mw @ Vector(v0))
                tris.append(mw @ Vector(v1))
                tris.append(mw @ Vector(v2))
        return tris

    def _commit(self, context):
        """Returns True (and finishes the operator) on a successful commit;
        False to stay in the modal so the user can retry immediately."""
        if not self._hover_selected:
            self.report({'INFO'}, "Hover the active tile's surface before clicking")
            return False
        loop = face_select.boundary_loop(self._mesh_faces, self._hover_selected)
        if loop is None:
            self.report({'WARNING'}, "Selection isn't a single simple region — "
                                     "try a smaller angle or a different face")
            return False
        pts_local = face_select.loop_to_xy(self._mesh_verts, loop)
        _commit_region(self._tile, pts_local)
        bpy.ops.ed.undo_push(message="HexFinity Flood Fill Region")
        self._finish(context)
        return True

    def _finish(self, context):
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)
        if context.area is not None:
            context.area.tag_redraw()

    def _draw_preview(self, context):
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None or not self._hover_tris_world:
            return
        pts2d = []
        for w in self._hover_tris_world:
            p = view3d_utils.location_3d_to_region_2d(region, rv3d, w)
            if p is None:
                return  # a vertex is behind the view this frame — skip drawing
            pts2d.append((p.x, p.y))

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float("color", (1.0, 0.8, 0.2, 0.35))
        batch_for_shader(shader, 'TRIS', {"pos": pts2d}).draw(shader)
        gpu.state.blend_set('NONE')


class HEXFINITY_OT_add_region(bpy.types.Operator):
    bl_idname = "hexfinity.add_region"
    bl_label = "Add Whole-Tile Region"
    bl_description = "Add a region covering the whole tile (no drawn outline)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.hexfinity_tile.is_generated

    def execute(self, context):
        obj = context.active_object
        tile = obj.hexfinity_tile
        region = tile.surface_regions.add()
        tile.active_surface_region_index = len(tile.surface_regions) - 1
        region.surface_type = _DEFAULT_SURFACE  # fires auto-fill + rebuild
        return {'FINISHED'}


class HEXFINITY_OT_remove_region(bpy.types.Operator):
    bl_idname = "hexfinity.remove_region"
    bl_label = "Remove Region"
    bl_description = "Remove the active surface region from this tile"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.hexfinity_tile.is_generated
                and len(obj.hexfinity_tile.surface_regions) > 0)

    def execute(self, context):
        obj = context.active_object
        tile = obj.hexfinity_tile
        idx = tile.active_surface_region_index
        if 0 <= idx < len(tile.surface_regions):
            tile.surface_regions.remove(idx)
            tile.active_surface_region_index = min(idx, len(tile.surface_regions) - 1)
            from .operators import rebuild_tile
            rebuild_tile(obj)
        return {'FINISHED'}


class HEXFINITY_UL_surface_regions(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        npts = len(item.points)
        shape = f"{npts}-pt loop" if npts >= 3 else "whole tile"
        name = item.name or item.surface_type.title()
        layout.label(text=f"{name}  ({shape})", icon='MESH_GRID')
