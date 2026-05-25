import math

import bpy
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from .mesh_builder import clamp_center_to_hexagon


def _build_uv_sphere_tris(segments=12, rings=6):
    """Unit-radius UV sphere as a flat triangle-vertex list for new_custom_shape."""
    verts = []
    for r in range(rings):
        phi0 = math.pi * (r / rings)
        phi1 = math.pi * ((r + 1) / rings)
        z0, z1 = math.cos(phi0), math.cos(phi1)
        r0, r1 = math.sin(phi0), math.sin(phi1)
        for s in range(segments):
            theta0 = 2.0 * math.pi * (s / segments)
            theta1 = 2.0 * math.pi * ((s + 1) / segments)
            c0, s0 = math.cos(theta0), math.sin(theta0)
            c1, s1 = math.cos(theta1), math.sin(theta1)
            v00 = Vector((r0 * c0, r0 * s0, z0))
            v01 = Vector((r0 * c1, r0 * s1, z0))
            v10 = Vector((r1 * c0, r1 * s0, z1))
            v11 = Vector((r1 * c1, r1 * s1, z1))
            verts.extend((v00, v10, v11))
            verts.extend((v00, v11, v01))
    return verts


_SPHERE_VERTS = _build_uv_sphere_tris()


def _mouse_on_plane(context, event, z_world):
    """Intersect the mouse ray with the horizontal world plane z=z_world.

    Returns a Vector, or None for degenerate side-on views where the ray is
    (near-)parallel to the plane.
    """
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


class HEXFINITY_GT_center_sphere(bpy.types.Gizmo):
    """World-aligned sphere gizmo that drags an XY "offset" target.

    Visual is a triangle-list custom shape so the sphere is drawn in the
    tile's local frame (not camera-facing). Modal drag projects the mouse
    ray onto the gizmo's initial horizontal plane and writes the result
    back via the "offset" target — same get/set callbacks the GizmoGroup
    wires up. Z of "offset" is left untouched; the centre clamp/write in
    `HEXFINITY_GGT_center._set_offset` already discards Z.

    Units: "offset" is mm-as-Blender-Units to match the rest of the
    plugin (`matrix_basis` is `obj.matrix_world`, which is mm here).
    """

    bl_idname = "HEXFINITY_GT_center_sphere"
    bl_target_properties = (
        {"id": "offset", "type": 'FLOAT', "array_length": 3},
    )

    __slots__ = (
        "custom_shape",
        "_init_offset",
        "_init_plane_z",
        "_init_mouse_world",
        "_inv_basis",
    )

    def setup(self):
        if not hasattr(self, "custom_shape"):
            self.custom_shape = self.new_custom_shape('TRIS', _SPHERE_VERTS)

    def draw(self, context):
        self.draw_custom_shape(self.custom_shape)

    def draw_select(self, context, select_id):
        self.draw_custom_shape(self.custom_shape, select_id=select_id)

    def invoke(self, context, event):
        self._init_offset = Vector(self.target_get_value("offset"))
        # Snapshot the basis so refresh() can't pull the inverse / plane out
        # from under us if it fires mid-drag.
        self._inv_basis = self.matrix_basis.inverted()
        self._init_plane_z = (self.matrix_basis @ self._init_offset).z
        self._init_mouse_world = _mouse_on_plane(context, event, self._init_plane_z)
        return {'RUNNING_MODAL'}

    def modal(self, context, event, tweak):
        if event.type == 'MOUSEMOVE':
            world_pos = _mouse_on_plane(context, event, self._init_plane_z)
            if world_pos is None or self._init_mouse_world is None:
                return {'RUNNING_MODAL'}
            # World mouse delta → local-frame delta via inverse basis. The
            # rigid-matrix translation cancels in the subtraction, so this
            # works for arbitrary tile rotations.
            init_local = self._inv_basis @ self._init_mouse_world
            curr_local = self._inv_basis @ world_pos
            new_offset = self._init_offset + (curr_local - init_local)
            self.target_set_value(
                "offset",
                (new_offset.x, new_offset.y, self._init_offset.z),
            )
            if context.area is not None:
                context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def exit(self, context, cancel):
        if cancel:
            self.target_set_value("offset", tuple(self._init_offset))


class HEXFINITY_GGT_center(bpy.types.GizmoGroup):
    """A sphere gizmo that drags the active tile's centre in the XY plane.

    The sphere hovers one level above the apex, mirroring the P1..P6
    corner-label idiom in `overlay.py`. Z of the centre is driven by the
    existing UI input (override toggle + center_level), not by the gizmo.
    The set callback discards any Z drag component, clamps the requested
    XY into the open hexagon, and writes the result back to the active
    object's per-Object property group — which triggers
    `_on_tile_local_update` in `properties.py`, which rebuilds the mesh in
    place via `operators.rebuild_tile`.

    The four linear params (diameter / level height / base thickness /
    subdivisions) are read from `scene.hexfinity_map` because they are
    map-wide invariants — see the plan in terrain_creation_initial.md.
    """

    bl_idname = "HEXFINITY_GGT_center"
    bl_label = "HexFinity Center"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'3D', 'PERSISTENT'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.hexfinity_tile.is_generated

    def setup(self, context):
        gz = self.gizmos.new(HEXFINITY_GT_center_sphere.bl_idname)
        gz.color = (0.2, 0.8, 1.0)
        gz.color_highlight = (0.5, 0.95, 1.0)
        gz.alpha = 0.6
        gz.alpha_highlight = 0.9
        gz.use_draw_modal = True
        gz.target_set_handler("offset", get=self._get_offset, set=self._set_offset)
        self._gz = gz

    def refresh(self, context):
        # Pin the gizmo's basis to the tile's world transform; matrix_offset
        # then places the sphere at (centre_xy, apex_z + level_height) in
        # tile-local mm. draw_custom_shape composes matrix_basis @ matrix_offset
        # for both the visual and the select-buffer pick, so this single
        # translation fixes both rendering position and clickability.
        obj = context.active_object
        p = obj.hexfinity_tile
        self._gz.matrix_basis = obj.matrix_world.copy()
        self._gz.matrix_offset = Matrix.Translation((
            p.center_x_mm, p.center_y_mm, self._apex_z_mm(p),
        ))
        # scale_basis is the sphere's world-space radius (unit-radius mesh),
        # so diameter = 2 * scale_basis. 1% of hex diameter ⇒ * 0.005.
        self._gz.scale_basis = context.scene.hexfinity_map.diameter_mm * 0.005

    def _get_offset(self):
        obj = bpy.context.active_object
        p = obj.hexfinity_tile
        return (p.center_x_mm, p.center_y_mm, self._apex_z_mm(p))

    def _set_offset(self, value):
        obj = bpy.context.active_object
        p = obj.hexfinity_tile
        map_props = bpy.context.scene.hexfinity_map
        x_mm, y_mm = clamp_center_to_hexagon(value[0], value[1], map_props.diameter_mm)
        # Writing the properties fires _on_tile_local_update on this tile,
        # which calls rebuild_tile. The operator's _REBUILDING guard handles
        # the re-entrant write-back of clamped values.
        p.center_x_mm = x_mm
        p.center_y_mm = y_mm

    @staticmethod
    def _apex_z_mm(p):
        # Hover one level above the apex so the sphere reads as a "handle"
        # for the centre — same +1 idiom as the P1..P6 labels in overlay.py.
        map_props = bpy.context.scene.hexfinity_map
        if p.override_center:
            level = max(0, p.center_level)
        else:
            level = (p.p1 + p.p2 + p.p3 + p.p4 + p.p5 + p.p6) / 6.0
        return map_props.base_thickness_mm + (level + 1) * map_props.level_height_mm
