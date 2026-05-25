import bpy


# ---------------------------------------------------------------------------
# Scene-level — the four global mesh params that every tile in the map
# shares (diameter, level height, base thickness, subdivisions), plus the
# grid extent and the root collection that owns the map.

def _on_global_update(self, context):
    # Lazy import — operators imports this module indirectly via the
    # package's register(), so importing at module load would cycle.
    from .operators import on_global_update
    try:
        on_global_update(self)
    except Exception:
        pass


class HexFinityMapProperties(bpy.types.PropertyGroup):
    is_generated: bpy.props.BoolProperty(
        name="Map Is Generated",
        default=False,
        options={'HIDDEN'},
    )
    diameter_mm: bpy.props.FloatProperty(
        name="Diameter (mm)",
        description="Point-to-point diameter of every hex tile in the map (uniform across the map)",
        default=100.0,
        min=0.001,
        soft_max=1000.0,
        update=_on_global_update,
    )
    level_height_mm: bpy.props.FloatProperty(
        name="Level Height (mm)",
        description="Vertical distance for one level step (uniform across the map)",
        default=5.0,
        min=0.001,
        soft_max=100.0,
        update=_on_global_update,
    )
    base_thickness_mm: bpy.props.FloatProperty(
        name="Base Thickness (mm)",
        description="Minimum gap between the bottom plane and the top surface (uniform across the map)",
        default=3.0,
        min=0.001,
        soft_max=100.0,
        update=_on_global_update,
    )
    subdivisions: bpy.props.IntProperty(
        name="Subdivisions",
        description="Number of cuts per top-triangle edge (uniform across the map)",
        default=4,
        min=0,
        soft_max=16,
        update=_on_global_update,
    )
    grid_x: bpy.props.IntProperty(
        name="X (columns)",
        description="Number of tile columns; 0 means generate a single tile at (0, 0)",
        default=5,
        min=0,
        soft_max=64,
    )
    grid_y: bpy.props.IntProperty(
        name="Y (rows)",
        description="Number of tile rows; 0 means generate a single tile at (0, 0)",
        default=5,
        min=0,
        soft_max=64,
    )
    root_collection: bpy.props.PointerProperty(
        name="Map Root Collection",
        type=bpy.types.Collection,
        options={'HIDDEN'},
    )


# ---------------------------------------------------------------------------
# Per-Object (tile) — corner levels, optional centre override, and the
# (q, r) coordinate the tile lives at within the map.

def _on_tile_local_update(self, context):
    # Centre/override/coord_x_y changes affect ONLY this tile — no
    # propagation. Same callback shape as the old _on_tile_prop_update.
    owner = self.id_data
    if not isinstance(owner, bpy.types.Object):
        return
    if not self.is_generated:
        return
    from .operators import rebuild_tile
    try:
        rebuild_tile(owner)
    except Exception:
        pass


def _make_corner_callback(corner_idx):
    """Build a per-corner update callback with the corner index closed over.

    Six of these (one per P1..P6) are wired into the IntProperty `update=`
    hooks below. Each delegates to operators.on_corner_changed, which
    propagates the new value to the shared corners on up to two neighbours.
    Using six wrappers (vs. detecting the changed corner inside one
    callback) keeps the propagation entry-point cheap and explicit.
    """
    def _cb(self, context):
        owner = self.id_data
        if not isinstance(owner, bpy.types.Object):
            return
        if not self.is_generated:
            return
        from .operators import on_corner_changed
        try:
            on_corner_changed(owner, corner_idx)
        except Exception:
            pass
    return _cb


_on_p1_update = _make_corner_callback(0)
_on_p2_update = _make_corner_callback(1)
_on_p3_update = _make_corner_callback(2)
_on_p4_update = _make_corner_callback(3)
_on_p5_update = _make_corner_callback(4)
_on_p6_update = _make_corner_callback(5)


class HexFinityProperties(bpy.types.PropertyGroup):
    is_generated: bpy.props.BoolProperty(
        name="Is Generated",
        description="Marks this Object as a HexFinity tile (set by the Generate operator).",
        default=False,
        options={'HIDDEN'},
    )
    coord_q: bpy.props.IntProperty(
        name="Coord Q",
        default=0,
        options={'HIDDEN'},
    )
    coord_r: bpy.props.IntProperty(
        name="Coord R",
        default=0,
        options={'HIDDEN'},
    )

    p1: bpy.props.IntProperty(name="P1", default=0, min=0, soft_max=20, update=_on_p1_update)
    p2: bpy.props.IntProperty(name="P2", default=0, min=0, soft_max=20, update=_on_p2_update)
    p3: bpy.props.IntProperty(name="P3", default=0, min=0, soft_max=20, update=_on_p3_update)
    p4: bpy.props.IntProperty(name="P4", default=0, min=0, soft_max=20, update=_on_p4_update)
    p5: bpy.props.IntProperty(name="P5", default=0, min=0, soft_max=20, update=_on_p5_update)
    p6: bpy.props.IntProperty(name="P6", default=0, min=0, soft_max=20, update=_on_p6_update)

    override_center: bpy.props.BoolProperty(
        name="Override Center Level",
        description="Pin the centre vertex to a specific level instead of using the corner mean",
        default=False,
        update=_on_tile_local_update,
    )
    center_level: bpy.props.IntProperty(
        name="Center Level",
        default=0,
        min=0,
        soft_max=20,
        update=_on_tile_local_update,
    )
    center_x_mm: bpy.props.FloatProperty(
        name="Center X (mm)",
        description="X offset of the apex from origin in millimetres",
        default=0.0,
        update=_on_tile_local_update,
    )
    center_y_mm: bpy.props.FloatProperty(
        name="Center Y (mm)",
        description="Y offset of the apex from origin in millimetres",
        default=0.0,
        update=_on_tile_local_update,
    )
