"""Path Segment authoring tool — the Settings panel's "Add Path Segment
Type" workflow.

Imports an externally-authored STL as a new or existing "segment type" (the
STL's parent folder name), lets the user draw connector waypoints on it
snapped to its footprint's convex-hull edges (mirroring
path_features.py's hex-edge snapping, but over an arbitrary convex
footprint instead of a fixed hexagon), and records the result into
settings.json for later use by a future path-splicing feature — not
designed here.

Mirrors the shell-over-bpy-free-math split used by regions.py/
path_features.py/flora.py: STL import, viewport setup, the Draw Path modal,
and settings.json's on-disk location resolution live here (bpy); convex
hull + generic hull-edge snap-point geometry live in segment_geometry.py,
and settings.json schema/read/write/dedup logic lives in
segment_settings.py (both bpy-free).

Workflow state (which STL is being authored, its hull, its drawn
waypoints) lives as real RNA data on the temporary imported object itself
(`obj.hexfinity_segment`) — the same "state lives on the real object"
convention terrain_lock.py uses for its Conform Lattice Edit/Apply/Cancel
flow — plus a scene-level pointer (`scene.hexfinity_segments.active_object`)
so every operator/panel draw can resolve the current subject without an
already-selected object to fall back on.

Nothing is written to settings.json until "Finish Add Segment" succeeds, so
a mid-workflow Cancel is always a pure in-scene no-op with no settings.json
rollback required.
"""

import math
import os

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

from .map import point_in_polygon
from . import segment_geometry
from . import segment_settings


SNAP_RADIUS_PX = 18.0
_LINE_COLOR = (0.85, 0.55, 0.25, 0.9)
_SNAP_COLOR = (0.3, 1.0, 0.5, 0.95)
_POINT_COLOR = (1.0, 1.0, 1.0, 1.0)

# Drawing-plane clearance above the segment's own highest vertex — reuses
# the 10 mm man-height convention path_features.py uses above a tile.
DRAW_PLANE_CLEARANCE_MM = 10.0

_ACTIVE = False


def is_active():
    return _ACTIVE


# ---------------------------------------------------------------------------
# settings.json location — Blender's per-user, update-safe writable-data
# directory for extensions, not the installed package's own (read-only /
# reinstall-wiped) assets/ folder.

def _settings_dir():
    return bpy.utils.extension_path_user(__package__, path="", create=True)


def _settings_path():
    return os.path.join(_settings_dir(), segment_settings.SETTINGS_FILENAME)


def ensure_settings_file():
    """Create settings.json with a fresh default document if it doesn't
    already exist. Called once from hexfinity.register() so "created if
    not existing... parsed upon plugin load if present" holds
    unconditionally, not just the first time the Settings box is used."""
    path = _settings_path()
    if not os.path.isfile(path):
        segment_settings.save_settings(path, segment_settings.default_settings())


def _load_settings():
    return segment_settings.load_settings(_settings_path())


# ---------------------------------------------------------------------------
# Workflow-object resolution (mirrors terrain_lock._resolve_conform_edit's
# self-healing shape, though a PointerProperty to an Object already clears
# itself to None when Blender deletes that object).

def _resolve_workflow(context):
    return context.scene.hexfinity_segments.active_object


def _cleanup_workflow(context, obj):
    context.scene.hexfinity_segments.active_object = None
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _find_view3d(context):
    window = context.window
    if window is None or window.screen is None:
        return None, None
    for area in window.screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    return area, region
    return None, None


def _frame_top_down(context, obj):
    """Select `obj` alone and snap the 3D viewport to a top-down orthographic
    view framing it. No existing helper for this in the codebase — every
    view-manipulation elsewhere only ever animates view_location
    (path_features._start_view_pan), never rotation/perspective."""
    area, region = _find_view3d(context)
    if area is None:
        return
    for o in context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    with context.temp_override(area=area, region=region):
        bpy.ops.view3d.view_axis(type='TOP')
        bpy.ops.view3d.view_selected()


# ---------------------------------------------------------------------------
# Add Path Segment Type: file browser -> confirm dialog -> STL import.

class HEXFINITY_OT_add_segment_type(bpy.types.Operator):
    bl_idname = "hexfinity.add_segment_type"
    bl_label = "Add Path Segment Type"
    bl_description = ("Import an STL as a new or existing path-segment "
                      "type. The STL's parent folder name becomes the type")
    bl_options = {'REGISTER', 'UNDO'}

    # Populated by the file browser (fileselect_add).
    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.stl", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_segments.active_object is None

    def invoke(self, context, event):
        data = _load_settings()
        last_dir = segment_settings.get_last_directory(data)
        if last_dir and os.path.isdir(last_dir):
            self.filepath = os.path.join(last_dir, "")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        filepath = self.filepath
        if not filepath or not filepath.lower().endswith(".stl"):
            self.report({'ERROR'}, "Select an .stl file.")
            return {'CANCELLED'}
        if not os.path.isfile(filepath):
            self.report({'ERROR'}, f"File not found: {filepath}")
            return {'CANCELLED'}

        type_name = os.path.basename(os.path.dirname(filepath))
        if not type_name:
            self.report({'ERROR'},
                        "Could not determine a type name from the file's folder.")
            return {'CANCELLED'}

        data = _load_settings()
        # The one write that isn't deferred to Finish — pure UX convenience,
        # not committed segment data, so there is nothing to roll back on a
        # later Cancel.
        segment_settings.set_last_directory(data, os.path.dirname(filepath))
        segment_settings.save_settings(_settings_path(), data)

        if segment_settings.find_segment(data, type_name, filepath) is not None:
            self.report({'ERROR'},
                        f"{os.path.basename(filepath)!r} is already registered "
                        f"under type {type_name!r}.")
            return {'CANCELLED'}

        type_exists = segment_settings.has_type(data, type_name)
        bpy.ops.hexfinity.confirm_add_segment_type(
            'INVOKE_DEFAULT', filepath=filepath, type_name=type_name,
            type_exists=type_exists)
        return {'FINISHED'}


class HEXFINITY_OT_segment_type_info(bpy.types.Operator):
    """Inert hover-info button — never actually executes anything. Its
    bl_description is the tooltip Blender shows on hover; drawn with
    icon='INFO', emboss=False, text="" wherever it appears."""
    bl_idname = "hexfinity.segment_type_info"
    bl_label = ""
    bl_description = ("Type = the STL's parent folder name, assumed "
                      "unique. Segments are assumed modeled at 10 mm "
                      "man-height; this tool does not rescale them")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        return {'CANCELLED'}


class HEXFINITY_OT_confirm_add_segment_type(bpy.types.Operator):
    bl_idname = "hexfinity.confirm_add_segment_type"
    bl_label = "Add Path Segment"
    bl_description = "Confirm importing this STL as a path-segment type/segment"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(options={'HIDDEN'})
    type_name: bpy.props.StringProperty(options={'HIDDEN'})
    type_exists: bpy.props.BoolProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        col.label(text=os.path.dirname(self.filepath), icon='FILE_FOLDER')
        col.label(text=os.path.basename(self.filepath), icon='MESH_DATA')
        col.separator()
        row = col.row(align=True)
        row.operator("hexfinity.segment_type_info", icon='INFO', text="",
                     emboss=False)
        row.label(text="Type = STL's folder name (unique). 10 mm man-height assumed.")
        col.separator()
        if self.type_exists:
            col.label(text=f"Will add a segment to existing type '{self.type_name}'.",
                      icon='ADD')
        else:
            col.label(text=f"Will create new type '{self.type_name}' and add this segment.",
                      icon='ADD')

    def execute(self, context):
        if not hasattr(bpy.ops.wm, "stl_import"):
            self.report({'ERROR'},
                        "STL importer (wm.stl_import) unavailable in this build.")
            return {'CANCELLED'}

        scene = context.scene
        before = set(scene.objects)
        try:
            bpy.ops.wm.stl_import(filepath=self.filepath)
        except RuntimeError as exc:
            self.report({'ERROR'}, f"STL import failed: {exc}")
            return {'CANCELLED'}
        imported = [o for o in scene.objects if o not in before]
        if not imported:
            self.report({'ERROR'}, "Import produced no objects.")
            return {'CANCELLED'}
        context.view_layer.update()

        obj = imported[0]
        if len(imported) > 1:
            for o in context.view_layer.objects:
                o.select_set(False)
            for o in imported:
                o.select_set(True)
            context.view_layer.objects.active = obj
            bpy.ops.object.join()
            obj = context.view_layer.objects.active

        obj.location = (0.0, 0.0, 0.0)
        obj.rotation_euler = (0.0, 0.0, 0.0)
        obj.scale = (1.0, 1.0, 1.0)
        context.view_layer.update()

        hull = segment_geometry.convex_hull(
            [(v.co.x, v.co.y) for v in obj.data.vertices])
        if len(hull) < 3:
            self.report({'ERROR'},
                        "The imported mesh's footprint is degenerate "
                        "(fewer than 3 hull points).")
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            return {'CANCELLED'}

        seg = obj.hexfinity_segment
        seg.type_name = self.type_name
        seg.source_filepath = self.filepath
        seg.type_is_new = not self.type_exists
        seg.hull.clear()
        for (x, y) in hull:
            p = seg.hull.add()
            p.x, p.y = x, y
        seg.waypoints.clear()
        seg.has_drawn_path = False

        obj.name = f"HexSegment_{self.type_name}"
        context.scene.hexfinity_segments.active_object = obj

        _frame_top_down(context, obj)

        self.report({'INFO'},
                    f"Imported {os.path.basename(self.filepath)} — draw its "
                    "path, then Finish Add Segment.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Draw Path — modal waypoint picker, copy-adapted from
# path_features.HEXFINITY_OT_draw_path_feature (ray-plane placement, screen-
# pixel snap radius, live gpu preview) but snapping to the segment's own
# convex-hull edges instead of a hex's, and with no feature_type/width/
# depth/preserve_edge/texture or multi-hex crossing/view-pan machinery —
# a segment has no neighbours.

def _draw_plane_z_local(obj):
    """Local z of the drawing plane: the segment's own highest vertex, plus
    clearance — mirrors path_features._feature_plane_z_local's "tallest
    corner + one man-height" shape, but measured off the mesh itself since
    a segment has no corner-level properties."""
    max_z = max((v.co.z for v in obj.data.vertices), default=0.0)
    return max_z + DRAW_PLANE_CLEARANCE_MM


def _mouse_on_plane(context, event, z_world):
    """Small local copy of gizmo._mouse_on_plane / path_features._mouse_on_
    plane — this codebase's established per-file-copy convention for this
    raycast helper (see also brush.py/flora.py)."""
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


def _hull_local(obj):
    return [(p.x, p.y) for p in obj.hexfinity_segment.hull]


def _snap_targets_world(obj, edge_snap, z_local):
    """[(Vector, edge_idx), ...] of every hull-edge snap point for a path
    drawn on `obj` — mirrors path_features._snap_targets_world, minus the
    "existing waypoint" targets (a segment's path is a single line, redrawn
    wholesale each time, not extended across multiple committed features)."""
    mw = obj.matrix_world
    targets = []
    for (x, y, edge_idx) in segment_geometry.hull_edge_snap_targets(
            _hull_local(obj), edge_snap):
        targets.append((mw @ Vector((x, y, z_local)), edge_idx))
    return targets


class HEXFINITY_OT_draw_segment_path(bpy.types.Operator):
    bl_idname = "hexfinity.draw_segment_path"
    bl_label = "Draw Path"
    bl_description = ("Click points above the loaded segment to draw its "
                      "connector waypoints, snapped to the footprint's hull "
                      "edges. Enter/RMB finishes (2+ points), Backspace "
                      "undoes a point, Esc cancels the draw")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return _resolve_workflow(context) is not None

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Draw Path must be started in the 3D viewport")
            return {'CANCELLED'}
        obj = _resolve_workflow(context)
        if obj is None:
            self.report({'ERROR'}, "No segment being authored.")
            return {'CANCELLED'}
        self._obj = obj
        self._pts_local = []    # [(x, y)] segment-local mm
        self._pts_world = []    # [Vector] for drawing
        self._edge_idxs = []    # parallel to _pts_local: hull edge index, or -1
        self._cursor = (event.mouse_region_x, event.mouse_region_y)
        self._snap_hint = None

        global _ACTIVE
        _ACTIVE = True

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Draw Segment Path:  LMB = add point (snaps to hull edge)    "
            "Enter/RMB = finish (2+ pts)    Backspace = undo point    "
            "Esc = cancel")
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
            self._add_point(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'BACK_SPACE', 'DEL'} and event.value == 'PRESS':
            if self._pts_local:
                self._pts_local.pop()
                self._pts_world.pop()
                self._edge_idxs.pop()
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
        edge_snap = context.scene.hexfinity_segments.edge_snap
        z_local = _draw_plane_z_local(self._obj)
        best = None
        best_dist = SNAP_RADIUS_PX
        for w, edge_idx in _snap_targets_world(self._obj, edge_snap, z_local):
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
            lp = self._obj.matrix_world.inverted() @ target
            self._pts_local.append((lp.x, lp.y))
            self._pts_world.append(target.copy())
            self._edge_idxs.append(edge_idx)
            return

        z_local = _draw_plane_z_local(self._obj)
        z_world = (self._obj.matrix_world @ Vector((0.0, 0.0, z_local))).z
        hit_pt = _mouse_on_plane(context, event, z_world)
        if hit_pt is None:
            self.report({'INFO'}, "Can't place a point from this viewing angle")
            return
        lp = self._obj.matrix_world.inverted() @ hit_pt
        if not point_in_polygon(lp.x, lp.y, _hull_local(self._obj)):
            self.report({'INFO'}, "Point must be inside the segment's footprint")
            return
        self._pts_local.append((lp.x, lp.y))
        self._pts_world.append(hit_pt.copy())
        self._edge_idxs.append(-1)

    def _close(self, context):
        if len(self._pts_local) < 2:
            self.report({'WARNING'}, "A path needs at least 2 points")
            return {'RUNNING_MODAL'}
        seg = self._obj.hexfinity_segment
        seg.waypoints.clear()
        for (x, y), edge_idx in zip(self._pts_local, self._edge_idxs):
            wp = seg.waypoints.add()
            wp.x, wp.y, wp.edge_idx = x, y, edge_idx
        seg.has_drawn_path = True
        bpy.ops.ed.undo_push(message="HexFinity Draw Segment Path")
        self._finish(context)
        return {'FINISHED'}

    def _finish(self, context):
        global _ACTIVE
        _ACTIVE = False
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

        preview = list(pts2d) + [tip]
        shader.uniform_float("color", _LINE_COLOR)
        batch_for_shader(shader, 'LINE_STRIP', {"pos": preview}).draw(shader)

        gpu.state.point_size_set(7.0)
        shader.uniform_float("color", tip_color)
        batch_for_shader(shader, 'POINTS', {"pos": [tip]}).draw(shader)
        if pts2d:
            shader.uniform_float("color", _POINT_COLOR)
            batch_for_shader(shader, 'POINTS', {"pos": pts2d}).draw(shader)

        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')


# ---------------------------------------------------------------------------
# Finish / Cancel.

class HEXFINITY_OT_finish_add_segment(bpy.types.Operator):
    bl_idname = "hexfinity.finish_add_segment"
    bl_label = "Finish Add Segment"
    bl_description = "Record the drawn path into settings.json and complete this segment"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _resolve_workflow(context)
        return obj is not None and obj.hexfinity_segment.has_drawn_path

    @staticmethod
    def _edge_waypoint_count(seg):
        return sum(1 for wp in seg.waypoints if wp.edge_idx >= 0)

    def invoke(self, context, event):
        obj = _resolve_workflow(context)
        if obj is None:
            self.report({'ERROR'}, "No segment being authored.")
            return {'CANCELLED'}
        seg = obj.hexfinity_segment
        edge_count = self._edge_waypoint_count(seg)
        if edge_count == 0:
            self.report({'ERROR'}, "At least one waypoint must be on the segment's edge.")
            return {'CANCELLED'}
        if edge_count == 1:
            return context.window_manager.invoke_confirm(
                self, event, title="This is an end segment.")
        return self.execute(context)

    def execute(self, context):
        obj = _resolve_workflow(context)
        if obj is None:
            self.report({'ERROR'}, "No segment being authored.")
            return {'CANCELLED'}
        seg = obj.hexfinity_segment
        edge_count = self._edge_waypoint_count(seg)
        if edge_count == 0:
            self.report({'ERROR'}, "At least one waypoint must be on the segment's edge.")
            return {'CANCELLED'}

        type_name = seg.type_name
        data = _load_settings()
        hull = [(p.x, p.y) for p in seg.hull]
        waypoints = [(wp.x, wp.y, wp.edge_idx) for wp in seg.waypoints]
        try:
            segment_settings.add_segment(
                data, type_name, seg.source_filepath, hull, waypoints,
                is_end_segment=(edge_count == 1),
                edge_snap=context.scene.hexfinity_segments.edge_snap,
            )
        except segment_settings.DuplicateSegmentError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        segment_settings.save_settings(_settings_path(), data)

        _cleanup_workflow(context, obj)
        self.report({'INFO'}, f"Added segment to type '{type_name}'.")
        return {'FINISHED'}


class HEXFINITY_OT_cancel_add_segment(bpy.types.Operator):
    bl_idname = "hexfinity.cancel_add_segment"
    bl_label = "Cancel"
    bl_description = ("Discard the in-progress segment — nothing has been "
                      "written to settings.json yet")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _resolve_workflow(context) is not None

    def execute(self, context):
        obj = _resolve_workflow(context)
        if obj is not None:
            _cleanup_workflow(context, obj)
        return {'FINISHED'}
