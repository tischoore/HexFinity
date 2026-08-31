"""HexFinity — modular hexagonal terrain map generator for Blender 5.1.

Packaged as a Blender extension (see blender_manifest.toml). The bpy imports
live in `properties`, `operators`, `panel`, and `gizmo` and are only loaded
from within `register()`, so `mesh_builder`, `manifold_check`, and `map`
remain importable from plain CPython for unit tests.
"""


def _classes():
    from . import (properties, operators, panel, gizmo, brush, regions,
                   scatter, flora, terrain_features)
    return (
        properties.HexFinityMapProperties,
        properties.HexFinitySurfacePoint,
        properties.HexFinitySurfaceRegion,
        properties.HexFinityFloraPlacement,
        properties.HexFinityTerrainFeature,
        properties.HexFinityProperties,
        properties.HexFinityBrushProperties,
        properties.HexFinityFloraProperties,
        properties.HexFinityTerrainProperties,
        properties.HexFinityTerrainFeatureProperties,
        operators.HEXFINITY_OT_generate_map,
        operators.HEXFINITY_OT_clear_map,
        operators.HEXFINITY_OT_import_terrain_object,
        operators.HEXFINITY_OT_redrop_terrain_object,
        operators.HEXFINITY_OT_generate_terrain_plateau,
        operators.HEXFINITY_OT_export_tiles,
        brush.HEXFINITY_OT_paint_brush,
        flora.HEXFINITY_OT_flora_marker,
        flora.HEXFINITY_OT_finalize_flora,
        regions.HEXFINITY_OT_draw_region,
        regions.HEXFINITY_OT_add_region,
        regions.HEXFINITY_OT_remove_region,
        regions.HEXFINITY_UL_surface_regions,
        terrain_features.HEXFINITY_OT_draw_terrain_feature,
        terrain_features.HEXFINITY_OT_remove_terrain_feature,
        terrain_features.HEXFINITY_OT_generate_terrain_features,
        terrain_features.HEXFINITY_UL_terrain_features,
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
    bpy.types.Scene.hexfinity_flora = bpy.props.PointerProperty(
        type=properties.HexFinityFloraProperties
    )
    bpy.types.Scene.hexfinity_terrain_features = bpy.props.PointerProperty(
        type=properties.HexFinityTerrainFeatureProperties
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
    if hasattr(bpy.types.Scene, "hexfinity_terrain_features"):
        del bpy.types.Scene.hexfinity_terrain_features
    if hasattr(bpy.types.Scene, "hexfinity_flora"):
        del bpy.types.Scene.hexfinity_flora
    if hasattr(bpy.types.Scene, "hexfinity_brush"):
        del bpy.types.Scene.hexfinity_brush
    if hasattr(bpy.types.Scene, "hexfinity_map"):
        del bpy.types.Scene.hexfinity_map
    for cls in reversed(_classes()):
        bpy.utils.unregister_class(cls)
