"""Flora marker — live preview of where a future scatter placement would land.

Foundation-only: this module places nothing. `HEXFINITY_OT_flora_marker` is a
modal operator (mirrors `brush.py`'s cursor-tracking + `regions.py`'s
raycast idiom) that raycasts the mouse onto the map every `MOUSEMOVE` and
draws a yellow circle-with-center-dot marker at the hit point via a
`POST_PIXEL` draw handler. Esc/RMB exits, same convention as the other
HexFinity picking tools.
"""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils


_MARKER_COLOR = (1.0, 0.9, 0.1, 0.9)
_MARKER_RADIUS_PX = 20.0

# Set/cleared by the modal operator's own lifecycle; read by panel.py so the
# sidebar can show a live indicator while the tool owns viewport input (the
# operator itself can't draw a clickable close button there, since a running
# modal operator swallows clicks before they reach panel buttons).
_ACTIVE = False


def is_active():
    return _ACTIVE


class HEXFINITY_OT_flora_marker(bpy.types.Operator):
    bl_idname = "hexfinity.flora_marker"
    bl_label = "Flora"
    bl_description = ("Move the mouse over the map to preview a flora placement "
                      "spot. RMB or Esc closes it.")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Flora must be started in the 3D viewport")
            return {'CANCELLED'}

        self._cursor = (event.mouse_region_x, event.mouse_region_y)
        self._hit_world = None

        global _ACTIVE
        _ACTIVE = True

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_marker, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "HexFinity Flora:  move to preview a spot    RMB / Esc = close")
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cursor = (event.mouse_region_x, event.mouse_region_y)
            self._update_hit(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._finish(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _update_hit(self, context, event):
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
        if hit and hit_obj is not None and hit_obj.original.hexfinity_tile.is_generated:
            self._hit_world = location.copy()
        else:
            self._hit_world = None

    def _finish(self, context):
        global _ACTIVE
        _ACTIVE = False
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)
        if context.area is not None:
            context.area.tag_redraw()

    def _draw_marker(self, context):
        if self._hit_world is None:
            return
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return
        center = view3d_utils.location_3d_to_region_2d(region, rv3d, self._hit_world)
        if center is None:
            return

        cx, cy = center.x, center.y
        r = _MARKER_RADIUS_PX
        n = 48
        pts = [(cx + r * math.cos(2.0 * math.pi * k / n),
                cy + r * math.sin(2.0 * math.pi * k / n)) for k in range(n)]
        pts.append(pts[0])

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        shader.bind()
        shader.uniform_float("color", _MARKER_COLOR)
        batch_for_shader(shader, 'LINE_STRIP', {"pos": pts}).draw(shader)

        gpu.state.point_size_set(7.0)
        shader.uniform_float("color", _MARKER_COLOR)
        batch_for_shader(shader, 'POINTS', {"pos": [(cx, cy)]}).draw(shader)

        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')
