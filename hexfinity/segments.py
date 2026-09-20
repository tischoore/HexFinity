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
import blf
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

from . import segment_geometry
from . import segment_settings


SNAP_RADIUS_PX = 18.0
_LINE_COLOR = (0.85, 0.55, 0.25, 0.9)
_SNAP_COLOR = (0.3, 1.0, 0.5, 0.95)
_POINT_COLOR = (1.0, 1.0, 1.0, 1.0)

# Persistent (post-modal) waypoint flags — drawn for the current Add Path
# Segment Type workflow object so a committed path stays visible once the
# Draw Path modal ends, mirroring overlay.py's "still visible after the
# modal draw operator exits" convention for Path Feature lines.
_FLAG_COLOR = _LINE_COLOR
_FLAG_ACTIVE_COLOR = _SNAP_COLOR
_FLAG_POLE_PX = 20.0
_FLAG_POLE_ACTIVE_PX = 30.0
_FLAG_WIDTH_PX = 10.0
_FLAG_WIDTH_ACTIVE_PX = 14.0
_FLAG_HEIGHT_PX = 7.0
_FLAG_HEIGHT_ACTIVE_PX = 10.0
_FLAG_FONT_ID = 0
_FLAG_FONT_SIZE = 12
_FLAG_TEXT_COLOR = (1.0, 1.0, 1.0, 1.0)
_FLAG_SHADOW_COLOR = (0.0, 0.0, 0.0, 0.9)

_DRAW_HANDLE = None
_CORNERS_DRAW_HANDLE = None

# Drawing-plane clearance above the segment's own highest vertex — reuses
# the 10 mm man-height convention path_features.py uses above a tile.
DRAW_PLANE_CLEARANCE_MM = 10.0

_ACTIVE = False
_CORNER_ACTIVE = False

# Distinct hover color for a sharp-corner snap hit during Add Corner, so it
# reads differently from a plain hull-edge snap (_SNAP_COLOR).
_SHARP_CORNER_COLOR = (1.0, 0.75, 0.1, 0.95)

# Persistent (post-modal) corner markers — a distinct glyph/color from the
# waypoint flags above so both overlays stay visually distinguishable when
# a segment has both corners and waypoints drawn simultaneously.
_CORNER_COLOR = (0.3, 0.6, 1.0, 0.9)
_CORNER_ACTIVE_COLOR = _SHARP_CORNER_COLOR
_CORNER_MARKER_PX = 6.0
_CORNER_MARKER_ACTIVE_PX = 9.0


def is_active():
    return _ACTIVE


def is_corner_active():
    return _CORNER_ACTIVE


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


def _corners_local(obj):
    return [(c.x, c.y) for c in obj.hexfinity_segment.corners]


def _footprint_local(obj):
    """The polygon Draw Path should snap to / test containment against:
    the user-authored Corners polygon once it has at least 3 points (it
    becomes the authoritative footprint at that point), else falls back to
    the auto-computed convex hull. Recomputed fresh on every call — nothing
    caches this, so corner edits made between Draw Path sessions are picked
    up automatically the next time it runs."""
    corners = _corners_local(obj)
    if len(corners) >= 3:
        return corners
    return _hull_local(obj)


def _snap_targets_world(obj, edge_snap, z_local):
    """[(Vector, edge_idx), ...] of every footprint-edge snap point for a
    path drawn on `obj` — mirrors path_features._snap_targets_world, minus
    the "existing waypoint" targets (a segment's path is a single line,
    redrawn wholesale each time, not extended across multiple committed
    features). Snaps against _footprint_local(obj) (the Corners polygon
    once authoritative, else the auto hull), plus — once corners are
    authoritative — an extra midpoint target per corner-to-corner edge, in
    addition to whatever edge_snap-density points hull_edge_snap_targets
    already produces (at edge_snap == 3 these coincide; the harmless
    duplicate is left as-is rather than deduped)."""
    mw = obj.matrix_world
    footprint = _footprint_local(obj)
    targets = []
    for (x, y, edge_idx) in segment_geometry.hull_edge_snap_targets(
            footprint, edge_snap):
        targets.append((mw @ Vector((x, y, z_local)), edge_idx))
    if len(obj.hexfinity_segment.corners) >= 3:
        for (x, y, edge_idx) in segment_geometry.polygon_edge_midpoints(footprint):
            targets.append((mw @ Vector((x, y, z_local)), edge_idx))
    return targets


def _hull_snap_targets_world(obj, edge_snap, z_local):
    """[(Vector, edge_idx), ...] of hull-edge snap points, always keyed off
    the auto-computed convex hull (never the Corners polygon) — used by
    HEXFINITY_OT_add_corner, since defining corners is the bootstrapping
    step and a corner can't usefully snap to the very polygon it's still
    building."""
    mw = obj.matrix_world
    return [
        (mw @ Vector((x, y, z_local)), edge_idx)
        for (x, y, edge_idx) in segment_geometry.hull_edge_snap_targets(
            _hull_local(obj), edge_snap)
    ]


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
        return _resolve_workflow(context) is not None and not is_corner_active()

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
            self._pts_local.append((lp.x, lp.y, lp.z))
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
        if not segment_geometry.point_in_polygon_concave(
                lp.x, lp.y, _footprint_local(self._obj)):
            self.report({'INFO'}, "Point must be inside the segment's footprint")
            return
        self._pts_local.append((lp.x, lp.y, lp.z))
        self._pts_world.append(hit_pt.copy())
        self._edge_idxs.append(-1)

    def _close(self, context):
        if len(self._pts_local) < 2:
            self.report({'WARNING'}, "A path needs at least 2 points")
            return {'RUNNING_MODAL'}
        seg = self._obj.hexfinity_segment
        seg.waypoints.clear()
        for (x, y, z), edge_idx in zip(self._pts_local, self._edge_idxs):
            wp = seg.waypoints.add()
            wp.x, wp.y, wp.z, wp.edge_idx = x, y, z, edge_idx
        seg.active_waypoint_index = 0
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
# Define Corners — one-shot Add Corner modal, structurally distinct from
# Draw Path's continuous multi-click session: pressing the button arms
# placement, the very next click places exactly one corner and the
# operator ends immediately, so the user re-presses the button for each
# subsequent corner. Snapping is always against the auto-computed hull
# (never the Corners polygon being built), plus a "sharp corner" hint —
# an extra, distinctly-colored snap target at any hull vertex whose turn
# exceeds scene.hexfinity_segments.sharp_corner_threshold_deg.

def _mesh_min_z_local(obj):
    """The whole mesh's minimum vertex Z, in local space — mirrors
    flora._get_or_import_mesh's min_z scan, used as a newly-placed corner's
    default Z (independent of where on the footprint it was clicked)."""
    return min((v.co.z for v in obj.data.vertices), default=0.0)


class HEXFINITY_OT_add_corner(bpy.types.Operator):
    bl_idname = "hexfinity.add_corner"
    bl_label = "Add Corner"
    bl_description = ("Click once to place a corner of the segment's "
                      "footprint polygon, snapped to the hull's edges/sharp "
                      "corners when close. Esc cancels without adding")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return _resolve_workflow(context) is not None and not is_active()

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Add Corner must be started in the 3D viewport")
            return {'CANCELLED'}
        obj = _resolve_workflow(context)
        if obj is None:
            self.report({'ERROR'}, "No segment being authored.")
            return {'CANCELLED'}
        self._obj = obj
        self._cursor = (event.mouse_region_x, event.mouse_region_y)
        self._snap_hint = None
        self._snap_is_sharp = False

        global _CORNER_ACTIVE
        _CORNER_ACTIVE = True

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Add Corner:  LMB = place    Esc = cancel")
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
            placed = self._place(context, event)
            self._finish(context)
            if placed:
                bpy.ops.ed.undo_push(message="HexFinity Add Corner")
                return {'FINISHED'}
            return {'CANCELLED'}

        if event.type == 'ESC' and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _find_snap_target(self, context, coord):
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return None, False
        edge_snap = context.scene.hexfinity_segments.edge_snap
        z_local = _draw_plane_z_local(self._obj)
        sharp_indices = {
            i for (_, _, i) in segment_geometry.sharp_hull_corner_points(
                _hull_local(self._obj),
                context.scene.hexfinity_segments.sharp_corner_threshold_deg)
        }
        best = None
        best_dist = SNAP_RADIUS_PX
        best_is_sharp = False
        for w, edge_idx in _hull_snap_targets_world(self._obj, edge_snap, z_local):
            s = view3d_utils.location_3d_to_region_2d(region, rv3d, w)
            if s is None:
                continue
            dist = math.hypot(s.x - coord[0], s.y - coord[1])
            if dist <= best_dist:
                best_dist = dist
                best = (w, edge_idx)
                best_is_sharp = edge_idx in sharp_indices
        return best, best_is_sharp

    def _update_snap_hint(self, context):
        self._snap_hint, self._snap_is_sharp = self._find_snap_target(context, self._cursor)

    def _place(self, context, event):
        coord = (event.mouse_region_x, event.mouse_region_y)
        hit, is_sharp = self._find_snap_target(context, coord)
        seg = self._obj.hexfinity_segment
        min_z_local = _mesh_min_z_local(self._obj)

        if hit is not None:
            target, edge_idx = hit
            lp = self._obj.matrix_world.inverted() @ target
            origin = 'SHARP_CORNER' if is_sharp else 'HULL_EDGE'
        else:
            z_local = _draw_plane_z_local(self._obj)
            z_world = (self._obj.matrix_world @ Vector((0.0, 0.0, z_local))).z
            hit_pt = _mouse_on_plane(context, event, z_world)
            if hit_pt is None:
                self.report({'INFO'}, "Can't place a point from this viewing angle")
                return False
            lp = self._obj.matrix_world.inverted() @ hit_pt
            if not segment_geometry.point_in_polygon_concave(lp.x, lp.y, _hull_local(self._obj)):
                self.report({'INFO'}, "Point must be inside the segment's footprint")
                return False
            edge_idx = -1
            origin = 'FREE'

        c = seg.corners.add()
        c.x, c.y, c.z = lp.x, lp.y, min_z_local
        c.edge_idx = edge_idx
        c.origin = origin
        seg.active_corner_index = len(seg.corners) - 1
        return True

    def _finish(self, context):
        global _CORNER_ACTIVE
        _CORNER_ACTIVE = False
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

        tip = self._cursor
        tip_color = _LINE_COLOR
        if self._snap_hint is not None:
            s = view3d_utils.location_3d_to_region_2d(region, rv3d, self._snap_hint[0])
            if s is not None:
                tip = (s.x, s.y)
                tip_color = _SHARP_CORNER_COLOR if self._snap_is_sharp else _SNAP_COLOR

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        shader.bind()

        gpu.state.point_size_set(9.0)
        shader.uniform_float("color", tip_color)
        batch_for_shader(shader, 'POINTS', {"pos": [tip]}).draw(shader)

        gpu.state.point_size_set(1.0)
        gpu.state.blend_set('NONE')


class HEXFINITY_UL_segment_corners(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        origin_label = {
            'FREE': "free", 'HULL_EDGE': "hull edge", 'SHARP_CORNER': "sharp corner",
        }.get(item.origin, item.origin)
        layout.label(
            text=(f"C{index + 1}  ({origin_label})  "
                  f"X:{item.x:.2f} Y:{item.y:.2f} Z:{item.z:.2f}"),
            icon='SNAP_VERTEX')


class HEXFINITY_OT_snap_corner_to_edge(bpy.types.Operator):
    bl_idname = "hexfinity.snap_corner_to_edge"
    bl_label = "Snap to Edge"
    bl_description = ("Move the selected corner onto the nearest point of "
                      "the segment's hull boundary (X/Y plane only) — "
                      "locked axes are held fixed")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _resolve_workflow(context)
        if obj is None:
            return False
        seg = obj.hexfinity_segment
        return 0 <= seg.active_corner_index < len(seg.corners)

    def execute(self, context):
        obj = _resolve_workflow(context)
        seg = obj.hexfinity_segment
        c = seg.corners[seg.active_corner_index]
        if c.lock_x and c.lock_y:
            self.report({'WARNING'}, "X and Y are both locked — nothing to move")
            return {'CANCELLED'}

        result = segment_geometry.nearest_point_on_hull_edge(
            c.x, c.y, _hull_local(obj), lock_x=c.lock_x, lock_y=c.lock_y)
        if result is None:
            self.report({'WARNING'}, "No hull edge crosses the locked coordinate")
            return {'CANCELLED'}

        c.x, c.y, c.edge_idx = result
        c.origin = 'HULL_EDGE'
        return {'FINISHED'}


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
        if obj is None:
            return False
        seg = obj.hexfinity_segment
        return seg.has_drawn_path and len(seg.corners) >= 3

    @staticmethod
    def _edge_waypoint_count(seg):
        return sum(1 for wp in seg.waypoints if wp.edge_idx >= 0)

    def invoke(self, context, event):
        obj = _resolve_workflow(context)
        if obj is None:
            self.report({'ERROR'}, "No segment being authored.")
            return {'CANCELLED'}
        seg = obj.hexfinity_segment
        if len(seg.corners) < 3:
            self.report({'ERROR'}, "At least 3 corners must be defined.")
            return {'CANCELLED'}
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
        if len(seg.corners) < 3:
            self.report({'ERROR'}, "At least 3 corners must be defined.")
            return {'CANCELLED'}
        edge_count = self._edge_waypoint_count(seg)
        if edge_count == 0:
            self.report({'ERROR'}, "At least one waypoint must be on the segment's edge.")
            return {'CANCELLED'}

        type_name = seg.type_name
        data = _load_settings()
        hull = [(p.x, p.y) for p in seg.hull]
        corners = [(c.x, c.y) for c in seg.corners]
        waypoints = [(wp.x, wp.y, wp.z, wp.edge_idx) for wp in seg.waypoints]
        try:
            segment_settings.add_segment(
                data, type_name, seg.source_filepath, hull, corners, waypoints,
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


class HEXFINITY_UL_segment_waypoints(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        edge_label = f"edge {item.edge_idx}" if item.edge_idx >= 0 else "interior"
        layout.label(
            text=(f"P{index + 1}  ({edge_label})  "
                  f"X:{item.x:.2f} Y:{item.y:.2f} Z:{item.z:.2f}"),
            icon='EMPTY_AXIS')


class HEXFINITY_OT_snap_waypoint_to_edge(bpy.types.Operator):
    bl_idname = "hexfinity.snap_waypoint_to_edge"
    bl_label = "Snap to Edge"
    bl_description = ("Move the selected waypoint onto the nearest point of "
                      "the segment's hull boundary (X/Y plane only) — "
                      "locked axes are held fixed")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _resolve_workflow(context)
        if obj is None:
            return False
        seg = obj.hexfinity_segment
        return 0 <= seg.active_waypoint_index < len(seg.waypoints)

    def execute(self, context):
        obj = _resolve_workflow(context)
        seg = obj.hexfinity_segment
        wp = seg.waypoints[seg.active_waypoint_index]
        if wp.lock_x and wp.lock_y:
            self.report({'WARNING'}, "X and Y are both locked — nothing to move")
            return {'CANCELLED'}

        result = segment_geometry.nearest_point_on_hull_edge(
            wp.x, wp.y, _hull_local(obj), lock_x=wp.lock_x, lock_y=wp.lock_y)
        if result is None:
            self.report({'WARNING'}, "No hull edge crosses the locked coordinate")
            return {'CANCELLED'}

        wp.x, wp.y, wp.edge_idx = result
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Manage Segments — a popup dialog (invoke_popup, not invoke_props_dialog,
# mirroring segment_path.HEXFINITY_OT_draw_segments_path_dialog's own choice
# to avoid that method's fixed OK/Cancel footer) listing every segment type
# in settings.json and, once one is picked, every segment registered under
# it, with per-row delete/reorder buttons. Every action writes to
# settings.json immediately (no separate "commit" step, matching
# set_last_directory's existing immediate-write convention) and then
# re-invokes the dialog so it always reflects the just-written state.

def _type_enum_items(self, context):
    data = _load_settings()
    names = segment_settings.list_types(data)
    if not names:
        return [('NONE', "No segment types in settings.json", "")]
    return [(name, name, "") for name in names]


class HEXFINITY_OT_manage_segments(bpy.types.Operator):
    bl_idname = "hexfinity.manage_segments"
    bl_label = "Manage Segments"
    bl_description = "Delete or reorder segments already registered in settings.json"
    bl_options = {'INTERNAL'}

    type_name: bpy.props.EnumProperty(items=_type_enum_items, name="Type")

    @classmethod
    def poll(cls, context):
        return _resolve_workflow(context) is None

    def invoke(self, context, event):
        data = _load_settings()
        if not segment_settings.list_types(data):
            self.report({'WARNING'}, "No segment types in settings.json yet.")
            return {'CANCELLED'}
        return context.window_manager.invoke_popup(self, width=420)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "type_name", text="")
        if self.type_name and self.type_name != 'NONE':
            data = _load_settings()
            segs = segment_settings.list_segments(data, self.type_name)
            col = layout.column(align=True)
            for i, entry in enumerate(segs):
                row = col.row(align=True)
                label = os.path.basename(entry.get("file", ""))
                if entry.get("is_end_segment"):
                    label += "  (end)"
                row.label(text=label)

                up_sub = row.row(align=True)
                up_sub.enabled = i > 0
                up = up_sub.operator("hexfinity.move_segment_entry", text="", icon='TRIA_UP')
                up.type_name, up.index, up.direction = self.type_name, i, -1

                down_sub = row.row(align=True)
                down_sub.enabled = i < len(segs) - 1
                down = down_sub.operator("hexfinity.move_segment_entry", text="", icon='TRIA_DOWN')
                down.type_name, down.index, down.direction = self.type_name, i, 1

                remove = row.operator("hexfinity.remove_segment_entry", text="", icon='X')
                remove.type_name, remove.index = self.type_name, i
        layout.separator()
        layout.operator("hexfinity.close_manage_segments_dialog", text="Close")

    def execute(self, context):
        # Never actually reached via the popup's own buttons — every
        # Operator needs one, mirroring
        # HEXFINITY_OT_draw_segments_path_dialog.execute's own rationale.
        return {'CANCELLED'}


class HEXFINITY_OT_close_manage_segments_dialog(bpy.types.Operator):
    """Inert "Close" button — mirrors HEXFINITY_OT_cancel_segments_path_dialog."""
    bl_idname = "hexfinity.close_manage_segments_dialog"
    bl_label = "Close"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        return {'CANCELLED'}


class HEXFINITY_OT_remove_segment_entry(bpy.types.Operator):
    bl_idname = "hexfinity.remove_segment_entry"
    bl_label = "Remove Segment"
    bl_description = "Delete this segment from settings.json"
    bl_options = {'INTERNAL'}

    type_name: bpy.props.StringProperty(options={'HIDDEN'})
    index: bpy.props.IntProperty(options={'HIDDEN'})

    def execute(self, context):
        data = _load_settings()
        try:
            segment_settings.remove_segment(data, self.type_name, self.index)
        except segment_settings.SettingsError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        segment_settings.save_settings(_settings_path(), data)
        bpy.ops.hexfinity.manage_segments('INVOKE_DEFAULT', type_name=self.type_name)
        return {'FINISHED'}


class HEXFINITY_OT_move_segment_entry(bpy.types.Operator):
    bl_idname = "hexfinity.move_segment_entry"
    bl_label = "Move Segment"
    bl_description = "Reorder this segment within its type"
    bl_options = {'INTERNAL'}

    type_name: bpy.props.StringProperty(options={'HIDDEN'})
    index: bpy.props.IntProperty(options={'HIDDEN'})
    direction: bpy.props.IntProperty(options={'HIDDEN'})

    def execute(self, context):
        data = _load_settings()
        try:
            segment_settings.move_segment(data, self.type_name, self.index, self.direction)
        except segment_settings.SettingsError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        segment_settings.save_settings(_settings_path(), data)
        bpy.ops.hexfinity.manage_segments('INVOKE_DEFAULT', type_name=self.type_name)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Persistent waypoint overlay — a SpaceView3D POST_PIXEL handler, separate
# from HEXFINITY_OT_draw_segment_path's own _draw (which only runs while
# that modal is live). Registered/unregistered from __init__.py's
# register()/unregister(), mirroring overlay.py's module-level handle
# pattern, so the committed path + flags stay visible in the viewport for
# as long as a segment is being authored (panel closed or not), and update
# immediately when a waypoint is edited or a different one is selected in
# the list (see properties._on_segment_waypoint_update).

def _draw_flag(shader, x, y, color, active):
    pole_h = _FLAG_POLE_ACTIVE_PX if active else _FLAG_POLE_PX
    flag_w = _FLAG_WIDTH_ACTIVE_PX if active else _FLAG_WIDTH_PX
    flag_h = _FLAG_HEIGHT_ACTIVE_PX if active else _FLAG_HEIGHT_PX
    top = y + pole_h

    shader.uniform_float("color", color)
    gpu.state.point_size_set(8.0 if active else 5.0)
    batch_for_shader(shader, 'POINTS', {"pos": [(x, y)]}).draw(shader)
    batch_for_shader(shader, 'LINES', {"pos": [(x, y), (x, top)]}).draw(shader)
    batch_for_shader(shader, 'TRIS', {"pos": [
        (x, top), (x + flag_w, top - flag_h * 0.5), (x, top - flag_h),
    ]}).draw(shader)


def _draw_committed_waypoints():
    context = bpy.context
    scene = context.scene
    if scene is None:
        return
    seg_props = getattr(scene, "hexfinity_segments", None)
    obj = seg_props.active_object if seg_props is not None else None
    if obj is None or is_active() or is_corner_active():
        return
    seg = obj.hexfinity_segment
    if not seg.has_drawn_path or len(seg.waypoints) == 0:
        return

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return

    mw = obj.matrix_world
    active_idx = seg.active_waypoint_index
    screen_pts = [
        view3d_utils.location_3d_to_region_2d(
            region, rv3d, mw @ Vector((wp.x, wp.y, wp.z)))
        for wp in seg.waypoints
    ]

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    shader.bind()

    line = [(s.x, s.y) for s in screen_pts if s is not None]
    if len(line) >= 2:
        shader.uniform_float("color", _FLAG_COLOR)
        batch_for_shader(shader, 'LINE_STRIP', {"pos": line}).draw(shader)

    for i, s in enumerate(screen_pts):
        if s is None:
            continue
        is_active_wp = (i == active_idx)
        _draw_flag(shader, s.x, s.y,
                   _FLAG_ACTIVE_COLOR if is_active_wp else _FLAG_COLOR,
                   is_active_wp)

    gpu.state.point_size_set(1.0)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')

    blf.size(_FLAG_FONT_ID, _FLAG_FONT_SIZE)
    blf.enable(_FLAG_FONT_ID, blf.SHADOW)
    blf.shadow(_FLAG_FONT_ID, 3, *_FLAG_SHADOW_COLOR)
    blf.shadow_offset(_FLAG_FONT_ID, 1, -1)
    blf.color(_FLAG_FONT_ID, *_FLAG_TEXT_COLOR)
    try:
        for i, s in enumerate(screen_pts):
            if s is None:
                continue
            pole_h = _FLAG_POLE_ACTIVE_PX if i == active_idx else _FLAG_POLE_PX
            blf.position(_FLAG_FONT_ID, s.x + 4.0, s.y + pole_h + 4.0, 0.0)
            blf.draw(_FLAG_FONT_ID, f"P{i + 1}")
    finally:
        blf.disable(_FLAG_FONT_ID, blf.SHADOW)


# ---------------------------------------------------------------------------
# Persistent corner overlay — a separate SpaceView3D POST_PIXEL handler from
# the waypoint one above, using a distinct diamond-marker glyph/color so
# corners and waypoints stay visually distinguishable when both are drawn
# for the same segment at once. Same bail-out rule as the waypoint overlay:
# any in-progress authoring modal (Draw Path OR Add Corner) owns the live
# overlay for its own session, so both persistent overlays suppress
# themselves while *either* is active, not just their own tool's modal.

def _draw_corner_marker(shader, x, y, color, active):
    r = _CORNER_MARKER_ACTIVE_PX if active else _CORNER_MARKER_PX
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'TRIS', {"pos": [
        (x, y + r), (x + r, y), (x, y - r),
    ]}).draw(shader)
    batch_for_shader(shader, 'TRIS', {"pos": [
        (x, y + r), (x, y - r), (x - r, y),
    ]}).draw(shader)


def _draw_committed_corners():
    context = bpy.context
    scene = context.scene
    if scene is None:
        return
    seg_props = getattr(scene, "hexfinity_segments", None)
    obj = seg_props.active_object if seg_props is not None else None
    if obj is None or is_active() or is_corner_active():
        return
    seg = obj.hexfinity_segment
    if len(seg.corners) == 0:
        return

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return

    mw = obj.matrix_world
    active_idx = seg.active_corner_index
    screen_pts = [
        view3d_utils.location_3d_to_region_2d(
            region, rv3d, mw @ Vector((c.x, c.y, c.z)))
        for c in seg.corners
    ]

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    shader.bind()

    line = [(s.x, s.y) for s in screen_pts if s is not None]
    if len(line) >= 2:
        closed = line + [line[0]]
        shader.uniform_float("color", _CORNER_COLOR)
        batch_for_shader(shader, 'LINE_STRIP', {"pos": closed}).draw(shader)

    for i, s in enumerate(screen_pts):
        if s is None:
            continue
        is_active_c = (i == active_idx)
        _draw_corner_marker(shader, s.x, s.y,
                             _CORNER_ACTIVE_COLOR if is_active_c else _CORNER_COLOR,
                             is_active_c)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')

    blf.size(_FLAG_FONT_ID, _FLAG_FONT_SIZE)
    blf.enable(_FLAG_FONT_ID, blf.SHADOW)
    blf.shadow(_FLAG_FONT_ID, 3, *_FLAG_SHADOW_COLOR)
    blf.shadow_offset(_FLAG_FONT_ID, 1, -1)
    blf.color(_FLAG_FONT_ID, *_FLAG_TEXT_COLOR)
    try:
        for i, s in enumerate(screen_pts):
            if s is None:
                continue
            r = _CORNER_MARKER_ACTIVE_PX if i == active_idx else _CORNER_MARKER_PX
            blf.position(_FLAG_FONT_ID, s.x + r + 2.0, s.y + r + 2.0, 0.0)
            blf.draw(_FLAG_FONT_ID, f"C{i + 1}")
    finally:
        blf.disable(_FLAG_FONT_ID, blf.SHADOW)


def register():
    global _DRAW_HANDLE, _CORNERS_DRAW_HANDLE
    if _DRAW_HANDLE is None:
        _DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_committed_waypoints, (), 'WINDOW', 'POST_PIXEL')
    if _CORNERS_DRAW_HANDLE is None:
        _CORNERS_DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_committed_corners, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _DRAW_HANDLE, _CORNERS_DRAW_HANDLE
    if _DRAW_HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLE, 'WINDOW')
        except (ValueError, RuntimeError):
            pass
        _DRAW_HANDLE = None
    if _CORNERS_DRAW_HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_CORNERS_DRAW_HANDLE, 'WINDOW')
        except (ValueError, RuntimeError):
            pass
        _CORNERS_DRAW_HANDLE = None
