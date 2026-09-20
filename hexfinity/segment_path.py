"""Draw Segments Path — the consumer of settings.json's segment_path
library (see segments.py/segment_geometry.py/segment_settings.py, the
"Add Path Segment Type" authoring tool). Places pre-built segment STLs
(bridges, junctions, etc.) across one or more selected hex tiles, snapping
connector waypoint to connector waypoint, and boolean-clipping a piece
that physically straddles a hex boundary into per-hex pieces — the same
INTERSECT-against-a-hex-prism pattern operators._cut_terrain_by_hex
already uses to split an overhanging terrain object.

Unlike a regular Path Feature (an open polyline carved as a heightmap
groove into the tile's own generated mesh, see path_features.py), a placed
segment is a real, separate mesh Object, parented under its tile exactly
like a terrain object or a scatter boulder — never fused into
mesh_builder.build_hex_tile's own manifold-checked pipeline. Each hex's
piece is still recorded as an entry in that tile's `path_features`
CollectionProperty (`feature_type == 'SEGMENT'`) purely so the existing
list UI / Remove button work unchanged; `path_features.path_specs()`
never sees these entries carve anything, since a SEGMENT feature's
`points` collection is always left empty.

Design decision (flagged in the implementation plan for review): a placed
piece stays rigid/horizontal — its Z is set once from wherever it was
snapped/dropped, with no per-vertex pitch/roll auto-tilt-to-terrain.
terrain_lock.py already tried and abandoned a single-planar-tilt auto-solve
for whole terrain objects as unreliable on real, non-planar scans; the same
judgement is reused here rather than reintroducing it for a smaller rigid
prefab piece. If a visibly-tilted fit across sloped corners turns out to
matter, that's a follow-up, not part of this pass.
"""

import math
import os
import time
import uuid

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

from .map import (edge_snap_points, corner_xy, EDGE_DIRECTIONS,
                  neighbour_coord, hex_prism_verts_faces, point_in_hex)


SNAP_RADIUS_PX = 18.0
ROTATION_STEP_DEG = 15.0
VIEW_PAN_DURATION_S = 1.2
# Generous vs. operators.SPLIT_PRISM_MARGIN_MM (5 mm) -- a bridge/junction
# segment can rise well above a flat terrain object's own bbox, and the
# boolean tool prism just needs to fully contain the piece being clipped.
SPLIT_PRISM_MARGIN_MM = 50.0

_GHOST_COLOR = (0.3, 0.7, 1.0, 0.35)
_GHOST_SNAP_COLOR = (0.35, 1.0, 0.55, 0.55)

# Custom id-properties stamped on every placed/clipped piece Object -- the
# "state lives on the real object" convention segments.py's own docstring
# already calls out for terrain_lock.py, reused here instead of more RNA on
# HexFinityPathFeature (which only gets segment_run_id/segment_piece).
SEGMENT_PIECE_TAG = "hf_segment_piece"
SEGMENT_TYPE_NAME = "hf_segment_type_name"
SEGMENT_SOURCE_FILE = "hf_segment_source_file"

_ACTIVE = False


def is_active():
    return _ACTIVE


# ---------------------------------------------------------------------------
# settings.json access + mesh import/cache -- mirrors flora.py's
# _get_or_import_mesh shape, keyed by the segment's own absolute filepath
# (an externally-referenced asset, not a bundled per-species folder).

def _load_types():
    from . import segments as seg_authoring
    data = seg_authoring._load_settings()
    return data["segment_path"]["types"]


_mesh_cache = {}   # filepath -> bpy.types.Mesh (shared, use_fake_user=True)


def _get_or_import_segment_mesh(filepath):
    mesh = _mesh_cache.get(filepath)
    if mesh is not None:
        if mesh.name in bpy.data.meshes:
            return mesh
        del _mesh_cache[filepath]

    if not hasattr(bpy.ops.wm, "stl_import") or not os.path.isfile(filepath):
        return None
    scene = bpy.context.scene
    before = set(scene.objects)
    try:
        bpy.ops.wm.stl_import(filepath=filepath)
    except RuntimeError:
        return None
    imported = [o for o in scene.objects if o not in before]
    if not imported:
        return None

    mesh = imported[0].data
    mesh.name = f"HF_Segment_{os.path.splitext(os.path.basename(filepath))[0]}"
    mesh.use_fake_user = True
    bpy.data.objects.remove(imported[0], do_unlink=True)
    for extra in imported[1:]:
        bpy.data.objects.remove(extra, do_unlink=True)

    _mesh_cache[filepath] = mesh
    return mesh


def _connector_locals(seg):
    """[(x_mm, y_mm), ...] of `seg`'s edge-tagged (edge_idx >= 0) waypoints
    -- its connection points, in the segment's own local mm space."""
    return [(wp["x_mm"], wp["y_mm"]) for wp in seg["waypoints"] if wp["edge_idx"] >= 0]


# ---------------------------------------------------------------------------
# Entry point: a small popup (Cancel / Draw) offering every type currently
# in settings.json.

def _type_items(self, context):
    types = _load_types()
    if not types:
        return [('NONE', "No segment types in settings.json", "")]
    return [(name, name, "") for name in sorted(types.keys())]


class HEXFINITY_OT_cancel_segments_path_dialog(bpy.types.Operator):
    """Inert Cancel button for the Draw Segments Path popup -- mirrors
    segments.HEXFINITY_OT_segment_type_info's "does nothing, just closes
    the popup" convention."""
    bl_idname = "hexfinity.cancel_segments_path_dialog"
    bl_label = "Cancel"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        return {'CANCELLED'}


class HEXFINITY_OT_draw_segments_path_dialog(bpy.types.Operator):
    bl_idname = "hexfinity.draw_segments_path_dialog"
    bl_label = "Draw Segments Path"
    bl_description = ("Pick a segment type, then click-place a chain of its "
                      "segments across the selected hexes")
    bl_options = {'INTERNAL'}

    type_name: bpy.props.EnumProperty(name="Type", items=_type_items)

    @classmethod
    def poll(cls, context):
        return any(o.hexfinity_tile.is_generated for o in context.selected_objects)

    def invoke(self, context, event):
        if not _load_types():
            self.report({'WARNING'},
                        "No segment types in settings.json -- use Add Path "
                        "Segment Type (Settings box) first.")
            return {'CANCELLED'}
        return context.window_manager.invoke_popup(self, width=260)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "type_name", text="")
        row = layout.row(align=True)
        row.operator("hexfinity.cancel_segments_path_dialog", text="Cancel")
        draw_op = row.operator("hexfinity.start_segments_path_draw", text="Draw")
        draw_op.type_name = self.type_name

    def execute(self, context):
        # invoke_popup draws no OK/Cancel footer of its own -- the two
        # explicit buttons in draw() are the real interaction. This
        # execute() only exists because Blender requires one on every
        # Operator; it's never reached via the popup's own buttons.
        return {'CANCELLED'}


# ---------------------------------------------------------------------------
# The modal placement operator.

class HEXFINITY_OT_start_segments_path_draw(bpy.types.Operator):
    bl_idname = "hexfinity.start_segments_path_draw"
    bl_label = "Draw Segments Path"
    bl_description = "Click-place segments of the chosen type across the selected hexes"
    bl_options = {'REGISTER'}

    type_name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return any(o.hexfinity_tile.is_generated for o in context.selected_objects)

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Draw Segments Path must be started in the 3D viewport")
            return {'CANCELLED'}
        types = _load_types()
        segments = types.get(self.type_name, {}).get("segments", [])
        if not segments:
            self.report({'ERROR'}, f"No segments registered under type {self.type_name!r}.")
            return {'CANCELLED'}

        self._selected_tiles = [o for o in context.selected_objects
                                if o.hexfinity_tile.is_generated]
        self._segments = segments
        self._variant_index = 0
        self._rotation_z = 0.0
        self._run_id = uuid.uuid4().hex
        self._cursor = (event.mouse_region_x, event.mouse_region_y)

        self._hover_tile = None
        self._hover_world = None
        self._transform = None          # (translation Vector, rotation_z float) or None
        self._snap_world = None
        self._snap_meta = None
        self._prev_open_world = None    # Vector or None -- open end of the running chain
        self._open_connectors = []      # [{"world": Vector, "tile": Object}, ...]

        self._ghost_tri_cache = {}      # filepath -> [(v0, v1, v2), ...] local verts
        self._pan_timer = None
        self._pan_start = None
        self._pan_target = None
        self._pan_start_time = 0.0

        global _ACTIVE
        _ACTIVE = True

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_VIEW')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Draw Segments Path:  LMB = place    Scroll = rotate    "
            "+/- = next/prev segment    RMB/Esc = finish")
        self._update_hover(context, event)
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    # -- modal event loop --------------------------------------------------

    def modal(self, context, event):
        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cursor = (event.mouse_region_x, event.mouse_region_y)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'WHEELUPMOUSE' and event.value == 'PRESS':
            self._rotation_z += math.radians(ROTATION_STEP_DEG)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'WHEELDOWNMOUSE' and event.value == 'PRESS':
            self._rotation_z -= math.radians(ROTATION_STEP_DEG)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type in {'NUMPAD_PLUS', 'EQUAL'} and event.value == 'PRESS':
            self._variant_index = (self._variant_index + 1) % len(self._segments)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type in {'NUMPAD_MINUS', 'MINUS'} and event.value == 'PRESS':
            self._variant_index = (self._variant_index - 1) % len(self._segments)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if context.region is None or context.region.type != 'WINDOW':
                return {'PASS_THROUGH'}
            self._try_place(context)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._finish(context)
            return {'FINISHED'}

        if event.type == 'TIMER':
            self._advance_view_pan(context)
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}

    @staticmethod
    def _tag_redraw(context):
        if context.area is not None:
            context.area.tag_redraw()

    # -- hover / snapping ----------------------------------------------------

    def _current_segment(self):
        return self._segments[self._variant_index]

    def _raycast_selected_tiles(self, context, coord):
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return None, None
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, location, _n, _i, hit_obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction)
        if not hit or hit_obj is None:
            return None, None
        tile = hit_obj.original
        if not tile.hexfinity_tile.is_generated or tile not in self._selected_tiles:
            return None, None
        return tile, location.copy()

    @staticmethod
    def _surface_z_at(context, tile, x, y):
        """Straight-down raycast onto `tile`'s own current surface at world
        (x, y) -- unlike path_features.py's floating "man height above the
        hex" draw plane, a segment is a physical object that must sit ON
        the tile, so every snap target needs a real surface Z, not a
        constant offset one."""
        depsgraph = context.evaluated_depsgraph_get()
        origin = Vector((x, y, tile.matrix_world.translation.z + 100000.0))
        direction = Vector((0.0, 0.0, -1.0))
        hit, location, _n, _i, hit_obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction)
        if hit and hit_obj is not None and hit_obj.original == tile:
            return location.z
        return tile.matrix_world.translation.z

    def _tile_edge_snap_targets(self, context, tile):
        """[(Vector, ('edge', edge_idx, tile)), ...] for `tile`'s own
        hex-edge points, Z resolved on the real surface."""
        map_props = context.scene.hexfinity_map
        mw = tile.matrix_world
        edge_snap = 3
        n = max(2, edge_snap)
        per_edge = n - 1
        out = []
        for i, (x, y) in enumerate(edge_snap_points(map_props.diameter_mm, edge_snap)):
            world_xy = mw @ Vector((x, y, 0.0))
            z = self._surface_z_at(context, tile, world_xy.x, world_xy.y)
            out.append((Vector((world_xy.x, world_xy.y, z)), ('edge', i, tile)))
        return out

    def _snap_targets(self, context, tile):
        targets = []
        if tile is not None:
            targets.extend(self._tile_edge_snap_targets(context, tile))
        for oc in self._open_connectors:
            targets.append((oc["world"], ('open', oc)))
        return targets

    def _update_hover(self, context, event):
        coord = (event.mouse_region_x, event.mouse_region_y)
        tile, hit_world = self._raycast_selected_tiles(context, coord)
        self._hover_tile = tile
        self._hover_world = hit_world
        self._snap_world = None
        self._snap_meta = None
        self._transform = None
        if hit_world is None:
            return

        seg = self._current_segment()
        connectors = _connector_locals(seg)
        cos_a, sin_a = math.cos(self._rotation_z), math.sin(self._rotation_z)

        def rotated(local_xy):
            x, y = local_xy
            return Vector((x * cos_a - y * sin_a, x * sin_a + y * cos_a, 0.0))

        translation = hit_world.copy()

        region = context.region
        rv3d = context.region_data
        targets = self._snap_targets(context, tile)
        best = None
        best_dist = SNAP_RADIUS_PX
        for local_xy in connectors:
            world_guess = translation + rotated(local_xy)
            s_guess = view3d_utils.location_3d_to_region_2d(region, rv3d, world_guess)
            if s_guess is None:
                continue
            for target_world, meta in targets:
                s_target = view3d_utils.location_3d_to_region_2d(region, rv3d, target_world)
                if s_target is None:
                    continue
                dist = math.hypot(s_target.x - s_guess.x, s_target.y - s_guess.y)
                if dist <= best_dist:
                    best_dist = dist
                    best = (local_xy, target_world, meta)

        if best is not None:
            local_xy, target_world, meta = best
            translation = target_world - rotated(local_xy)
            self._snap_world = target_world
            self._snap_meta = meta

        self._transform = (translation, self._rotation_z)

    def _connects_to_prev(self):
        if self._prev_open_world is None:
            return True  # the very first piece of the run needs no connection
        if self._snap_meta is None or self._snap_meta[0] != 'open':
            return False
        oc = self._snap_meta[1]
        return (oc["world"] - self._prev_open_world).length < 1e-3

    # -- placement -----------------------------------------------------------

    def _try_place(self, context):
        if self._transform is None:
            self.report({'INFO'}, "Hover over a selected hex to place")
            return
        if not self._connects_to_prev():
            self.report({'WARNING'},
                        "Snap a connector waypoint to the previous segment's "
                        "open end before placing")
            return
        seg = self._current_segment()
        translation, rot = self._transform
        self._commit_piece(context, seg, translation, rot)

    @staticmethod
    def _world_bbox(obj):
        pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        zs = [p.z for p in pts]
        return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))

    def _clip_to_hexes(self, context, obj, map_props):
        """Boolean-INTERSECT `obj` against every selected tile's hex prism it
        overlaps (bbox pre-filtered, mirrors operators._hex_split_candidates),
        exactly like operators._cut_terrain_by_hex splits an overhanging
        terrain object. Returns [(tile, piece_obj), ...]; `obj` itself is
        reused directly (no boolean) when only one hex is involved, or
        consumed (removed) once its clipped pieces exist."""
        bmin, bmax = self._world_bbox(obj)
        half = map_props.diameter_mm * 0.5
        candidates = []
        for tile in self._selected_tiles:
            tx, ty = tile.location.x, tile.location.y
            if (bmax.x < tx - half or bmin.x > tx + half
                    or bmax.y < ty - half or bmin.y > ty + half):
                continue
            candidates.append(tile)
        if not candidates:
            return []
        if len(candidates) == 1:
            return [(candidates[0], obj)]

        coll = map_props.root_collection
        z_min = bmin.z - SPLIT_PRISM_MARGIN_MM
        z_max = bmax.z + SPLIT_PRISM_MARGIN_MM
        pieces = []
        for tile in candidates:
            tx, ty = tile.location.x, tile.location.y
            verts, faces = hex_prism_verts_faces(map_props.diameter_mm, z_min, z_max)
            verts = [(x + tx, y + ty, z) for x, y, z in verts]
            prism_mesh = bpy.data.meshes.new("hf_segment_prism_tmp")
            prism_mesh.from_pydata(verts, [], faces)
            prism_mesh.update(calc_edges=True)
            prism_obj = bpy.data.objects.new("hf_segment_prism_tmp", prism_mesh)
            coll.objects.link(prism_obj)

            dup_mesh = obj.data.copy()
            dup_obj = bpy.data.objects.new(f"HF_SegmentPiece_{tile.name}", dup_mesh)
            coll.objects.link(dup_obj)
            dup_obj.matrix_world = obj.matrix_world.copy()

            mod = dup_obj.modifiers.new(name="hf_segment_cut", type='BOOLEAN')
            mod.operation = 'INTERSECT'
            mod.solver = 'EXACT'
            mod.object = prism_obj
            with context.temp_override(active_object=dup_obj,
                                       selected_objects=[dup_obj],
                                       object=dup_obj):
                bpy.ops.object.modifier_apply(modifier=mod.name)

            bpy.data.objects.remove(prism_obj, do_unlink=True)
            bpy.data.meshes.remove(prism_mesh)

            if len(dup_obj.data.polygons) == 0:
                bpy.data.objects.remove(dup_obj, do_unlink=True)
                bpy.data.meshes.remove(dup_mesh)
                continue
            pieces.append((tile, dup_obj))

        # Only consume `obj` once it actually produced at least one non-empty
        # piece — a bbox pre-filter can admit a candidate tile whose real
        # (non-axis-aligned) hex prism the piece never actually overlaps, so
        # every intersection can come out empty even with >1 candidate. In
        # that case `obj` must survive for the caller's own "no pieces"
        # cleanup (_commit_piece) — removing it unconditionally here left
        # that cleanup trying to remove an already-freed object.
        if pieces:
            bpy.data.objects.remove(obj, do_unlink=True)
        return pieces

    @staticmethod
    def _shared_edge_idx(tile_a, tile_b):
        qa, ra = tile_a.hexfinity_tile.coord_q, tile_a.hexfinity_tile.coord_r
        qb, rb = tile_b.hexfinity_tile.coord_q, tile_b.hexfinity_tile.coord_r
        for i, d in enumerate(EDGE_DIRECTIONS):
            if neighbour_coord(qa, ra, d) == (qb, rb):
                return i
        return None

    def _edge_endpoints_world(self, context, tile, edge_idx):
        map_props = context.scene.hexfinity_map
        mw = tile.matrix_world
        a = corner_xy(edge_idx, map_props.diameter_mm)
        b = corner_xy((edge_idx + 1) % 6, map_props.diameter_mm)
        return mw @ Vector((a[0], a[1], 0.0)), mw @ Vector((b[0], b[1], 0.0))

    @staticmethod
    def _closest_point_on_segment(p, a, b):
        ab = b - a
        t = (p - a).dot(ab) / max(ab.length_squared, 1e-9)
        t = max(0.0, min(1.0, t))
        return a + ab * t

    @staticmethod
    def _point_in_tile_local(tile, world_point, diameter_mm):
        lp = tile.matrix_world.inverted() @ world_point
        return point_in_hex(lp.x, lp.y, diameter_mm)

    def _commit_piece(self, context, seg, translation, rot):
        mesh = _get_or_import_segment_mesh(seg["file"])
        if mesh is None:
            self.report({'ERROR'}, f"Could not import {seg['file']!r}")
            return

        map_props = context.scene.hexfinity_map
        coll = map_props.root_collection
        obj = bpy.data.objects.new("HF_SegmentPiece", mesh)
        coll.objects.link(obj)
        obj.rotation_euler = (0.0, 0.0, rot)
        obj.location = translation
        context.view_layer.update()

        cos_a, sin_a = math.cos(rot), math.sin(rot)
        connectors_world = [
            translation + Vector((x * cos_a - y * sin_a, x * sin_a + y * cos_a, 0.0))
            for (x, y) in _connector_locals(seg)
        ]
        consumed = self._prev_open_world
        remaining = [w for w in connectors_world
                    if consumed is None or (w - consumed).length > 1e-3]

        pieces = self._clip_to_hexes(context, obj, map_props)
        if not pieces:
            bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'WARNING'}, "Segment falls outside every selected hex")
            return

        crossing_neighbour = None
        if len(pieces) == 2:
            (tile_a, _obj_a), (tile_b, _obj_b) = pieces
            edge_idx = self._shared_edge_idx(tile_a, tile_b)
            if edge_idx is not None:
                a1, a2 = self._edge_endpoints_world(context, tile_a, edge_idx)
                crossing_xy = self._closest_point_on_segment(translation, a1, a2)
                crossing_z = self._surface_z_at(context, tile_a, crossing_xy.x, crossing_xy.y)
                crossing_point = Vector((crossing_xy.x, crossing_xy.y, crossing_z))
                remaining.append(crossing_point)
                if tile_b in self._selected_tiles:
                    crossing_neighbour = tile_b

        from . import properties

        run_open_connectors = []
        for tile, piece_obj in pieces:
            piece_obj[SEGMENT_PIECE_TAG] = True
            piece_obj[SEGMENT_TYPE_NAME] = self.type_name
            piece_obj[SEGMENT_SOURCE_FILE] = seg["file"]
            world = piece_obj.matrix_world.copy()
            piece_obj.parent = tile
            piece_obj.matrix_parent_inverse = tile.matrix_world.inverted()
            piece_obj.matrix_world = world

            feature = tile.hexfinity_tile.path_features.add()
            feature.name = f"Segment {len(tile.hexfinity_tile.path_features)}"
            properties._PATH_FEATURE_FILLING = True
            try:
                feature.feature_type = 'SEGMENT'
            finally:
                properties._PATH_FEATURE_FILLING = False
            feature.segment_run_id = self._run_id
            feature.segment_piece = piece_obj
            tile.hexfinity_tile.active_path_feature_index = (
                len(tile.hexfinity_tile.path_features) - 1)

            for w in remaining:
                if self._point_in_tile_local(tile, w, map_props.diameter_mm):
                    run_open_connectors.append({"world": w, "tile": tile})

        self._open_connectors = run_open_connectors
        bpy.ops.ed.undo_push(message="HexFinity Draw Segments Path")

        if self._open_connectors:
            self._prev_open_world = self._open_connectors[-1]["world"]
            next_tile = self._open_connectors[-1]["tile"]
        else:
            self._prev_open_world = None
            next_tile = pieces[-1][0]

        if crossing_neighbour is not None and crossing_neighbour is not self._hover_tile:
            context.view_layer.objects.active = crossing_neighbour
            self._start_view_pan(context, crossing_neighbour)
        elif next_tile is not None and next_tile is not context.view_layer.objects.active:
            context.view_layer.objects.active = next_tile

    # -- view pan on hex crossing (mirrors path_features.py's own copy) ------

    def _start_view_pan(self, context, tile_obj):
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
        eased = t * t * (3.0 - 2.0 * t)
        rv3d.view_location = self._pan_start.lerp(self._pan_target, eased)
        self._tag_redraw(context)
        if t >= 1.0:
            self._stop_view_pan(context)

    def _stop_view_pan(self, context):
        if self._pan_timer is not None:
            context.window_manager.event_timer_remove(self._pan_timer)
            self._pan_timer = None
        self._pan_start = None
        self._pan_target = None

    # -- teardown / ghost drawing ---------------------------------------------

    def _finish(self, context):
        global _ACTIVE
        _ACTIVE = False
        self._stop_view_pan(context)
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)
        self._tag_redraw(context)

    def _local_triangles(self, filepath):
        cached = self._ghost_tri_cache.get(filepath)
        if cached is not None:
            return cached
        mesh = _get_or_import_segment_mesh(filepath)
        tris = []
        if mesh is not None:
            mesh.calc_loop_triangles()
            for lt in mesh.loop_triangles:
                tris.append(tuple(mesh.vertices[i].co.copy() for i in lt.vertices))
        self._ghost_tri_cache[filepath] = tris
        return tris

    def _draw(self, context):
        if self._transform is None:
            return
        translation, rot = self._transform
        seg = self._current_segment()
        tris = self._local_triangles(seg["file"])
        if not tris:
            return

        cos_a, sin_a = math.cos(rot), math.sin(rot)

        def to_world(v):
            x = v.x * cos_a - v.y * sin_a
            y = v.x * sin_a + v.y * cos_a
            return (translation.x + x, translation.y + y, translation.z + v.z)

        positions = []
        for tri in tris:
            for v in tri:
                positions.append(to_world(v))

        color = _GHOST_SNAP_COLOR if self._snap_world is not None else _GHOST_COLOR
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.face_culling_set('NONE')
        shader.bind()
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'TRIS', {"pos": positions}).draw(shader)
        gpu.state.depth_test_set('NONE')
        gpu.state.face_culling_set('NONE')
        gpu.state.blend_set('NONE')
