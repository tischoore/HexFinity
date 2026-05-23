"""HexFinity — modular hexagonal terrain tile generator for Blender 5.1.

Packaged as a Blender extension (see blender_manifest.toml). The bpy imports
live in `properties`, `operators`, `panel` and are only loaded from within
`register()`, so `mesh_builder` and `manifold_check` remain importable from
plain CPython for unit tests.
"""


def _classes():
    from . import properties, operators, panel, gizmo
    return (
        properties.HexFinityProperties,
        operators.HEXFINITY_OT_generate,
        panel.HEXFINITY_PT_panel,
        gizmo.HEXFINITY_GGT_center,
    )


def register():
    import bpy
    from . import properties
    for cls in _classes():
        bpy.utils.register_class(cls)
    bpy.types.Object.hexfinity_tile = bpy.props.PointerProperty(
        type=properties.HexFinityProperties
    )


def unregister():
    import bpy
    if hasattr(bpy.types.Object, "hexfinity_tile"):
        del bpy.types.Object.hexfinity_tile
    for cls in reversed(_classes()):
        bpy.utils.unregister_class(cls)
