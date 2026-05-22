import bpy

from .mesh_builder import clamp_center_to_hexagon


class HEXFINITY_GGT_center(bpy.types.GizmoGroup):
    """A 2D ring gizmo that drags the tile center in the XY plane.

    Z of the center is driven by the existing UI input (override toggle +
    center_level), not by the gizmo. The set callback discards any Z drag
    component, clamps the requested XY into the open hexagon, and writes
    the result back to the scene property group — which triggers the
    property update callback in `properties.py`, which rebuilds the mesh
    in place via `operators.rebuild_tile_if_active`.
    """

    bl_idname = "HEXFINITY_GGT_center"
    bl_label = "HexFinity Center"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'3D', 'PERSISTENT'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.get("hexfinity_tile") == 1

    def setup(self, context):
        gz = self.gizmos.new("GIZMO_GT_move_3d")
        gz.draw_style = 'RING_2D'
        gz.color = (0.2, 0.8, 1.0)
        gz.color_highlight = (0.5, 0.95, 1.0)
        gz.alpha = 0.6
        gz.alpha_highlight = 0.9
        gz.scale_basis = 0.15
        gz.use_draw_modal = True
        gz.target_set_handler("offset", get=self._get_offset, set=self._set_offset)
        self._gz = gz

    def refresh(self, context):
        # matrix_basis is identity so the gizmo's "offset" target is the
        # absolute world-space position (in metres). The getter parks it at
        # the current apex; the setter receives the dragged-to position.
        self._gz.matrix_basis.identity()

    def _get_offset(self):
        p = bpy.context.scene.hexfinity
        return (p.center_x_mm / 1000.0, p.center_y_mm / 1000.0, self._apex_z_m(p))

    def _set_offset(self, value):
        p = bpy.context.scene.hexfinity
        x_mm = value[0] * 1000.0
        y_mm = value[1] * 1000.0
        x_mm, y_mm = clamp_center_to_hexagon(x_mm, y_mm, p.diameter_mm)
        # Writing the properties fires the update callback, which calls
        # rebuild_tile_if_active. The operator's _REBUILDING guard handles
        # the re-entrant write-back of clamped values.
        p.center_x_mm = x_mm
        p.center_y_mm = y_mm

    @staticmethod
    def _apex_z_m(p):
        # Best-effort apex Z for gizmo placement; doesn't affect mesh build.
        if p.override_center:
            level = max(0, p.center_level)
        else:
            level = (p.p1 + p.p2 + p.p3 + p.p4 + p.p5 + p.p6) / 6.0
        return (p.base_thickness_mm + level * p.level_height_mm) / 1000.0
