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
        default=220.0,
        min=0.001,
        soft_max=1000.0,
        update=_on_global_update,
    )
    level_height_mm: bpy.props.FloatProperty(
        name="Level Height (mm)",
        description="Vertical distance for one level step (uniform across the map)",
        default=10.0,
        min=0.001,
        soft_max=100.0,
        update=_on_global_update,
    )
    base_thickness_mm: bpy.props.FloatProperty(
        name="Base Thickness (mm)",
        description="Minimum gap between the bottom plane and the top surface (uniform across the map). Must be at least 10 mm so the inter-tile tab/hole interlock fits inside the base",
        default=10.0,
        min=10.0,
        soft_max=100.0,
        update=_on_global_update,
    )
    smoothness_passes: bpy.props.IntProperty(
        name="Smoothness Passes",
        description="Loop subdivision iterations applied to the top surface "
                    "(shape detail and smoothness). 2 passes = 288 tris/tile, "
                    "3 = 1152, 4 = 4608 — bump until the dome looks right",
        default=2,
        min=0,
        soft_max=5,
        update=_on_global_update,
    )
    resample_density: bpy.props.IntProperty(
        name="Resample Density",
        description="Extra linear-midpoint subdivision after Loop smoothing. "
                    "Adds polycount via chord midpoints; does NOT introduce "
                    "new smoothing (use this for downstream displacement)",
        default=0,
        min=0,
        soft_max=3,
        update=_on_global_update,
    )
    base_level: bpy.props.IntProperty(
        name="Base Level",
        default=0,
        min=0,
        soft_max=20,
        description="Starting corner level for every tile when the map is generated. "
                    "Raising it lets you carve depressions down to the base thickness "
                    "(level 0) — no lower. Applied on (re)generate only.",
    )
    man_height_mm: bpy.props.FloatProperty(
        name="Man Height (mm)",
        description="Printed height of a human figure — the model scale reference. "
                    "Procedural surface feature sizes (cobble/gravel/furrow pitch) "
                    "default to their real-world size scaled by this. 28 mm ~ common "
                    "wargaming scale. Editable per region afterwards.",
        default=28.0,
        min=0.1,
        soft_max=200.0,
        subtype='DISTANCE',
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
# Scene-level — terrain brush settings. Read by the modal paint operator each
# dab; no update callbacks (changing a brush setting never rebuilds a tile).

class HexFinityBrushProperties(bpy.types.PropertyGroup):
    radius_mm: bpy.props.FloatProperty(
        name="Radius (mm)",
        description="Brush size — top verts within this distance of the cursor are displaced",
        default=40.0,
        min=0.1,
        soft_max=500.0,
    )
    strength_mm: bpy.props.FloatProperty(
        name="Strength (mm/s)",
        description="Millimetres of displacement applied per second of dragging at the brush centre",
        default=20.0,
        min=0.0,
        soft_max=200.0,
    )
    direction: bpy.props.EnumProperty(
        name="Direction",
        description="Whether a stroke raises or lowers the surface",
        items=[
            ('RAISE', "Raise", "Push the surface up"),
            ('LOWER', "Lower", "Pull the surface down"),
        ],
        default='RAISE',
    )
    preserve_edge: bpy.props.BoolProperty(
        name="Preserve Edge",
        description="Damp the brush to ~0 near a hex rim so the straight edges and "
                    "shared corners stay put. Turn OFF to let a stroke flow across a "
                    "seam onto the neighbouring tile",
        default=True,
    )
    edge_falloff_mm: bpy.props.FloatProperty(
        name="Edge Falloff (mm)",
        description="Width of the rim damping band when Preserve Edge is on",
        default=20.0,
        min=0.001,
        soft_max=200.0,
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


# ---------------------------------------------------------------------------
# Per-tile procedural surface regions. A region is a closed loop (polygon) drawn
# on the tile top in tile-local mm, plus a surface type, its params, and a main
# direction (used by anisotropic surfaces like furrows). Multiple regions per
# tile are supported via a CollectionProperty on HexFinityProperties.

# Guard: while auto-filling a region's params on a type change, suppress the
# per-field rebuilds so the dropdown triggers a single rebuild, not several.
_REGION_FILLING = False


def _on_region_update(self, context):
    if _REGION_FILLING:
        return
    owner = self.id_data
    if not isinstance(owner, bpy.types.Object):
        return
    tile = getattr(owner, "hexfinity_tile", None)
    if tile is None or not tile.is_generated:
        return
    from .operators import rebuild_tile
    try:
        rebuild_tile(owner)
    except Exception:
        pass


def _on_region_type_update(self, context):
    # Pick sensible mm defaults for the chosen surface from the registry +
    # man-height, then rebuild once.
    global _REGION_FILLING
    from . import procedural_surfaces as ps
    surf = ps.SURFACES.get(self.surface_type)
    _REGION_FILLING = True
    try:
        d = ps.feature_mm_default(self.surface_type,
                                  context.scene.hexfinity_map.man_height_mm)
        if d > 0.0:
            self.feature_mm = d
        if surf is not None:
            self.depth_mm = surf.default_depth_mm
            self.regularity = surf.default_regularity
    finally:
        _REGION_FILLING = False
    _on_region_update(self, context)


def _surface_type_items(self, context):
    from . import procedural_surfaces
    return procedural_surfaces.enum_items()


class HexFinitySurfacePoint(bpy.types.PropertyGroup):
    x: bpy.props.FloatProperty(name="X", default=0.0)
    y: bpy.props.FloatProperty(name="Y", default=0.0)


class HexFinitySurfaceRegion(bpy.types.PropertyGroup):
    surface_type: bpy.props.EnumProperty(
        name="Surface",
        description="Procedural surface applied inside this region",
        items=_surface_type_items,
        update=_on_region_type_update,
    )
    feature_mm: bpy.props.FloatProperty(
        name="Feature Size (mm)",
        description="Characteristic feature size — cobble width / pebble size / "
                    "furrow pitch. Auto-filled from Man Height; editable here",
        default=2.0,
        min=0.001,
        soft_max=100.0,
        subtype='DISTANCE',
        update=_on_region_update,
    )
    depth_mm: bpy.props.FloatProperty(
        name="Depth (mm)",
        description="Peak-to-trough height of the texture",
        default=2.0,
        min=0.0,
        soft_max=50.0,
        subtype='DISTANCE',
        update=_on_region_update,
    )
    regularity: bpy.props.FloatProperty(
        name="Regularity",
        description="0 = irregular/varied, 1 = uniform. For cobblestone this is "
                    "the cell jitter (neat courses vs random cobbles)",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_on_region_update,
    )
    direction_deg: bpy.props.FloatProperty(
        name="Direction (deg)",
        description="Main direction for anisotropic surfaces (e.g. the way "
                    "furrows run). Ignored by isotropic surfaces",
        default=0.0,
        update=_on_region_update,
    )
    mask_falloff_mm: bpy.props.FloatProperty(
        name="Edge Blend (mm)",
        description="Width of the soft band that blends the texture in at the "
                    "region boundary. 0 = hard edge",
        default=5.0,
        min=0.0,
        soft_max=50.0,
        subtype='DISTANCE',
        update=_on_region_update,
    )
    points: bpy.props.CollectionProperty(type=HexFinitySurfacePoint)


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
    dome_area: bpy.props.FloatProperty(
        name="Dome Area",
        description="Radial position of the inner Q ring between the centre and the rim midpoint. "
                    "Low = narrow tight dome; high = broad dome reaching toward the rim. "
                    "Right-click → Copy to Selected to apply to multiple selected tiles.",
        default=0.5,
        min=0.1,
        max=0.9,
        subtype='FACTOR',
        update=_on_tile_local_update,
    )
    dome_damping: bpy.props.FloatProperty(
        name="Dome Damping",
        description="How much the inner Q ring is pulled away from the centre toward the corner-edge midpoint. "
                    "0 = Q sticks to the centre (flat-topped plateau); 1 = Q pulled fully to corner-edge midpoint "
                    "(sharp peak with flat skirt). Default 2/3 reproduces the current smooth dome exactly. "
                    "Right-click → Copy to Selected to apply to multiple selected tiles.",
        default=2.0 / 3.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_on_tile_local_update,
    )
    local_subdiv: bpy.props.IntProperty(
        name="Local Subdivision",
        description="Extra linear-midpoint subdivision applied to just this tile, "
                    "on top of the map-wide Resample Density. Adds local mesh "
                    "density only (no extra smoothing). Each pass QUADRUPLES this "
                    "tile's top faces (4^n), so cost explodes: ~2-3 is plenty for "
                    "cobblestone, ~4 for sharp tiles/cracks; beyond that a single "
                    "tile reaches hundreds of thousands of verts. Changing it clears "
                    "any painted/snapped displacement on this tile.",
        default=0,
        min=0,
        soft_max=6,
        update=_on_tile_local_update,
    )
    surface_regions: bpy.props.CollectionProperty(type=HexFinitySurfaceRegion)
    active_surface_region_index: bpy.props.IntProperty(
        name="Active Region",
        default=0,
        options={'HIDDEN'},
    )


# ---------------------------------------------------------------------------
# Per-Object (loaded terrain model) — the snap-to-model slider. Lives on the
# imported mesh, not the tile; changing it reshapes whichever hex the model
# sits over (see operators.apply_terrain_snap).

def _on_terrain_snap_update(self, context):
    owner = self.id_data
    if not isinstance(owner, bpy.types.Object):
        return
    from .operators import apply_terrain_snap
    try:
        apply_terrain_snap(owner, self.snap_mm)
    except Exception:
        pass


class HexFinityTerrainProperties(bpy.types.PropertyGroup):
    snap_mm: bpy.props.IntProperty(
        name="Terrain snap to model",
        description="Move the hex top surface under the model's flat base toward "
                    "the base by this many millimetres, both up and down. "
                    "0 = no snapping; verts stop once they reach the base "
                    "(0.2 mm overlap). Arch openings and overhangs are left alone.",
        default=0,
        min=0,
        soft_max=50,
        step=1,
        subtype='NONE',
        update=_on_terrain_snap_update,
    )
    snap_damp_mm: bpy.props.FloatProperty(
        name="Snap damping (mm)",
        description="Blend the snap into the surrounding terrain over this width "
                    "in millimetres: the terrain around the model ramps smoothly "
                    "up/down to the base (organic skirt) instead of a hard cliff "
                    "at the footprint edge. 0 = no skirt. Faded near the hex rim "
                    "so tile seams stay aligned.",
        default=0.0,
        min=0.0,
        soft_max=100.0,
        subtype='NONE',
        update=_on_terrain_snap_update,
    )
    # Remembers which tile this model last snapped, so moving it to another hex
    # clears the stale offset from the old one (operators.apply_terrain_snap).
    snap_tile: bpy.props.PointerProperty(
        type=bpy.types.Object,
        options={'HIDDEN'},
    )
