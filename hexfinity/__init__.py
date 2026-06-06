"""HexFinity — modular hexagonal terrain map generator for Blender 5.1.

Packaged as a Blender extension (see blender_manifest.toml). The bpy imports
live in `properties`, `operators`, `panel`, and `gizmo` and are only loaded
from within `register()`, so `mesh_builder`, `manifold_check`, and `map`
remain importable from plain CPython for unit tests.
"""


def _classes():
    from . import properties, operators, panel, gizmo, brush, regions, scatter
    return (
        properties.HexFinityMapProperties,
        properties.HexFinitySurfacePoint,
        properties.HexFinitySurfaceRegion,
        properties.HexFinityProperties,
        properties.HexFinityBrushProperties,
        properties.HexFinityTerrainProperties,
        operators.HEXFINITY_OT_generate_map,
        operators.HEXFINITY_OT_regenerate_map,
        operators.HEXFINITY_OT_import_terrain_object,
        operators.HEXFINITY_OT_redrop_terrain_object,
        operators.HEXFINITY_OT_export_tiles,
        brush.HEXFINITY_OT_paint_brush,
        regions.HEXFINITY_OT_draw_region,
        regions.HEXFINITY_OT_add_region,
        regions.HEXFINITY_OT_remove_region,
        regions.HEXFINITY_UL_surface_regions,
        scatter.HEXFINITY_OT_merge_scatter,
        panel.HEXFINITY_PT_panel,
        gizmo.HEXFINITY_GT_center_sphere,
        gizmo.HEXFINITY_GGT_center,
    )


def register():
    import bpy
    from . import properties, overlay
    for cls in _classes():
        bpy.utils.register_class(cls)
    bpy.types.Scene.hexfinity_map = bpy.props.PointerProperty(
        type=properties.HexFinityMapProperties
    )
    bpy.types.Scene.hexfinity_brush = bpy.props.PointerProperty(
        type=properties.HexFinityBrushProperties
    )
    bpy.types.Object.hexfinity_tile = bpy.props.PointerProperty(
        type=properties.HexFinityProperties
    )
    bpy.types.Object.hexfinity_terrain = bpy.props.PointerProperty(
        type=properties.HexFinityTerrainProperties
    )
    overlay.register()


def unregister():
    import bpy
    from . import overlay
    overlay.unregister()
    if hasattr(bpy.types.Object, "hexfinity_terrain"):
        del bpy.types.Object.hexfinity_terrain
    if hasattr(bpy.types.Object, "hexfinity_tile"):
        del bpy.types.Object.hexfinity_tile
    if hasattr(bpy.types.Scene, "hexfinity_brush"):
        del bpy.types.Scene.hexfinity_brush
    if hasattr(bpy.types.Scene, "hexfinity_map"):
        del bpy.types.Scene.hexfinity_map
    for cls in reversed(_classes()):
        bpy.utils.unregister_class(cls)
