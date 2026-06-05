"""Terrain brush — sculpt-style paint that pushes the top surface up/down.

HexFinity is fully regenerative: a tile's mesh is rebuilt from a tiny parameter
set on every edit. The brush therefore stores its result as *data* — a
per-top-vertex z-offset array on the Object (`obj["hf_brush_disp"]`) — that
`operators.rebuild_tile` re-applies through `build_hex_tile` on every rebuild.
This module only contains the bpy modal operator (live drag, cursor draw, and
the dab maths); the displacement application + clamp + manifold check all live in
the bpy-free builder.

Live feedback during a drag mutates `obj.data.vertices[i].co.z` directly for
instant response; the authoritative, manifold-checked state is produced by a
single `rebuild_tile` per stroke on mouse-up.
"""

import math
import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

from .mesh_builder import rim_edge_distance, top_vertex_count


def _smooth_falloff(t):
    """1 at the brush centre, 0 at the rim, smooth (C1) at both ends.

    `t` is normalised distance (0 at centre, 1 at radius); values outside
    [0, 1] are clamped by the caller before this is reached.
    """
    s = t * t * (3.0 - 2.0 * t)  # smoothstep 0→1
    return 1.0 - s


def _ntop_for(obj, map_props):
    """Top-vertex count for `obj` — the cached value `rebuild_tile` stores,
    falling back to the closed-form count from the map's subdivision globals."""
    ntop = obj.get("hf_brush_ntop")
    if ntop is None:
        ntop = top_vertex_count(map_props.smoothness_passes,
                                map_props.resample_density)
    return int(ntop)


class HEXFINITY_OT_paint_brush(bpy.types.Operator):
    bl_idname = "hexfinity.paint_brush"
    bl_label = "Paint Terrain"
    bl_description = ("Drag on the tiles to raise or lower the top surface. "
                     "Left-drag paints, right-click or Esc exits the brush.")
    # Undo is managed per-stroke via ed.undo_push on mouse-up (one undo step
    # per stroke); the modal session itself is not a single registered op.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    # -- lifecycle ---------------------------------------------------------

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Terrain brush must be started in the 3D viewport")
            return {'CANCELLED'}

        self._painting = False
        self._last_time = 0.0
        self._touched = []          # objects painted in the current stroke
        self._work = {}             # obj -> working displacement list[float]
        self._cursor = (event.mouse_region_x, event.mouse_region_y)
        self._screen_radius = 30.0  # px; refined once we have a surface hit

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_cursor, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "HexFinity brush:  Left-drag = paint    RMB / Esc = exit")
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        # Let the user navigate (orbit / pan / zoom) without painting.
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cursor = (event.mouse_region_x, event.mouse_region_y)
            if self._painting:
                now = time.time()
                dt = min(now - self._last_time, 0.1)
                self._last_time = now
                self._apply_dab(context, event, dt)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                # Only start a stroke when the cursor is over the 3D view.
                if context.region is None or context.region.type != 'WINDOW':
                    return {'PASS_THROUGH'}
                self._painting = True
                self._last_time = time.time()
                self._touched = []
                self._work = {}
                # A bare click still leaves a small dab.
                self._apply_dab(context, event, 1.0 / 60.0)
                if context.area is not None:
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            elif event.value == 'RELEASE':
                if self._painting:
                    self._end_stroke(context)
                return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            if self._painting:
                self._end_stroke(context)
            self._finish(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _finish(self, context):
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)
        if context.area is not None:
            context.area.tag_redraw()

    # -- painting ----------------------------------------------------------

    def _candidate_tiles(self, context, hit_world):
        """Generated tiles whose footprint can overlap the brush at `hit_world`.

        Cull by tile-origin distance: a tile can only be touched if its centre
        is within the brush radius plus the hex circumradius of the hit point.
        Keeps cross-seam strokes correct (neighbours are included) without a map
        adjacency walk.
        """
        map_props = context.scene.hexfinity_map
        coll = map_props.root_collection
        if coll is None:
            return []
        brush = context.scene.hexfinity_brush
        reach = brush.radius_mm + map_props.diameter_mm / 2.0
        out = []
        for obj in coll.objects:
            if not obj.hexfinity_tile.is_generated:
                continue
            d = (obj.matrix_world.translation - hit_world).length
            if d <= reach:
                out.append(obj)
        return out

    def _apply_dab(self, context, event, dt):
        region = context.region
        rv3d = context.region_data
        if region is None or region.type != 'WINDOW' or rv3d is None:
            return
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

        depsgraph = context.evaluated_depsgraph_get()
        hit, location, _normal, _index, hit_obj, _matrix = context.scene.ray_cast(
            depsgraph, origin, direction)
        if not hit or hit_obj is None:
            return
        if not hit_obj.original.hexfinity_tile.is_generated:
            return

        self._screen_radius = self._world_radius_to_px(context, region, rv3d, location)

        map_props = context.scene.hexfinity_map
        brush = context.scene.hexfinity_brush
        radius = brush.radius_mm
        diameter = map_props.diameter_mm
        base_z = map_props.base_thickness_mm
        sign = 1.0 if brush.direction == 'RAISE' else -1.0
        step = sign * brush.strength_mm * dt
        if step == 0.0 or radius <= 0.0:
            return

        for obj in self._candidate_tiles(context, location):
            ntop = _ntop_for(obj, map_props)
            mesh = obj.data
            if len(mesh.vertices) < ntop:
                continue
            work = self._work.get(obj.name)
            if work is None:
                stored = obj.get("hf_brush_disp")
                if stored is not None and len(stored) == ntop:
                    work = list(stored)
                else:
                    work = [0.0] * ntop
                self._work[obj.name] = work
                self._touched.append(obj)

            inv = obj.matrix_world.inverted()
            lp = inv @ location
            changed = False
            for i in range(ntop):
                co = mesh.vertices[i].co
                dx = co.x - lp.x
                dy = co.y - lp.y
                dist = math.hypot(dx, dy)
                if dist >= radius:
                    continue
                delta = step * _smooth_falloff(dist / radius)
                if brush.preserve_edge:
                    d_edge = rim_edge_distance(co.x, co.y, diameter)
                    edge_factor = max(0.0, min(1.0, d_edge / brush.edge_falloff_mm))
                    delta *= edge_factor
                if delta == 0.0:
                    continue
                work[i] += delta
                new_z = co.z + delta
                if new_z < base_z:
                    new_z = base_z
                co.z = new_z
                changed = True
            if changed:
                mesh.update()

    def _end_stroke(self, context):
        """Bake the stroke: write each touched tile's working displacement to its
        Object and rebuild it once (authoritative + manifold-checked). One undo
        step covers the whole stroke."""
        from .operators import rebuild_tile
        for obj in self._touched:
            obj["hf_brush_disp"] = self._work[obj.name]
        for obj in self._touched:
            try:
                rebuild_tile(obj)
            except Exception as exc:  # pragma: no cover - defensive
                self.report({'WARNING'}, f"HexFinity brush rebuild failed: {exc}")
        if self._touched:
            bpy.ops.ed.undo_push(message="HexFinity Terrain Brush")
        self._painting = False
        self._touched = []
        self._work = {}

    # -- cursor drawing ----------------------------------------------------

    @staticmethod
    def _world_radius_to_px(context, region, rv3d, center_world):
        """Pixel length of the brush radius at the hit point, for the cursor."""
        cam_right = rv3d.view_matrix.inverted().col[0].xyz
        brush = context.scene.hexfinity_brush
        edge_world = center_world + cam_right * brush.radius_mm
        c2d = view3d_utils.location_3d_to_region_2d(region, rv3d, center_world)
        e2d = view3d_utils.location_3d_to_region_2d(region, rv3d, edge_world)
        if c2d is None or e2d is None:
            return 30.0
        return max(4.0, (e2d - c2d).length)

    def _draw_cursor(self, context):
        # Registered on the WINDOW region only, so it always fires in the right
        # space; the passed-in context is the invoke-time one (its `.region` may
        # be the N-panel the operator was launched from) so we must not gate on
        # it here.
        cx, cy = self._cursor
        r = self._screen_radius
        n = 48
        pts = [(cx + r * math.cos(2.0 * math.pi * k / n),
                cy + r * math.sin(2.0 * math.pi * k / n)) for k in range(n)]
        pts.append(pts[0])

        brush = context.scene.hexfinity_brush
        color = (0.3, 0.9, 1.0, 0.9) if brush.direction == 'RAISE' \
            else (1.0, 0.5, 0.3, 0.9)

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": pts})
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')
