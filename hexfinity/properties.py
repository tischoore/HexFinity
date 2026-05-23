import bpy


def _on_tile_prop_update(self, context):
    # Lazy import — operators imports properties indirectly via the package
    # init, so importing it at module load would create a cycle.
    owner = self.id_data
    if not isinstance(owner, bpy.types.Object):
        return
    if not self.is_generated:
        return
    from .operators import rebuild_tile
    try:
        rebuild_tile(owner, self)
    except Exception:
        # Per-edit rebuilds must not crash the UI; failures surface on next
        # explicit Generate.
        pass


class HexFinityProperties(bpy.types.PropertyGroup):
    is_generated: bpy.props.BoolProperty(
        name="Is Generated",
        description="Marks this Object as a HexFinity tile (set by the Generate operator).",
        default=False,
        options={'HIDDEN'},
    )
    diameter_mm: bpy.props.FloatProperty(
        name="Diameter (mm)",
        description="Point-to-point diameter of the hex tile in millimetres",
        default=100.0,
        min=0.001,
        soft_max=1000.0,
        update=_on_tile_prop_update,
    )
    level_height_mm: bpy.props.FloatProperty(
        name="Level Height (mm)",
        description="Vertical distance for one level step",
        default=5.0,
        min=0.001,
        soft_max=100.0,
        update=_on_tile_prop_update,
    )
    base_thickness_mm: bpy.props.FloatProperty(
        name="Base Thickness (mm)",
        description="Minimum gap between the bottom plane and the top surface",
        default=3.0,
        min=0.001,
        soft_max=100.0,
        update=_on_tile_prop_update,
    )
    p1: bpy.props.IntProperty(name="P1", default=0, min=0, soft_max=20, update=_on_tile_prop_update)
    p2: bpy.props.IntProperty(name="P2", default=0, min=0, soft_max=20, update=_on_tile_prop_update)
    p3: bpy.props.IntProperty(name="P3", default=0, min=0, soft_max=20, update=_on_tile_prop_update)
    p4: bpy.props.IntProperty(name="P4", default=0, min=0, soft_max=20, update=_on_tile_prop_update)
    p5: bpy.props.IntProperty(name="P5", default=0, min=0, soft_max=20, update=_on_tile_prop_update)
    p6: bpy.props.IntProperty(name="P6", default=0, min=0, soft_max=20, update=_on_tile_prop_update)
    override_center: bpy.props.BoolProperty(
        name="Override Center Level",
        description="Pin the centre vertex to a specific level instead of using the corner mean",
        default=False,
        update=_on_tile_prop_update,
    )
    center_level: bpy.props.IntProperty(
        name="Center Level",
        default=0,
        min=0,
        soft_max=20,
        update=_on_tile_prop_update,
    )
    center_x_mm: bpy.props.FloatProperty(
        name="Center X (mm)",
        description="X offset of the apex from origin in millimetres",
        default=0.0,
        update=_on_tile_prop_update,
    )
    center_y_mm: bpy.props.FloatProperty(
        name="Center Y (mm)",
        description="Y offset of the apex from origin in millimetres",
        default=0.0,
        update=_on_tile_prop_update,
    )
    subdivisions: bpy.props.IntProperty(
        name="Subdivisions",
        description="Number of cuts per top-triangle edge",
        default=4,
        min=0,
        soft_max=16,
        update=_on_tile_prop_update,
    )
