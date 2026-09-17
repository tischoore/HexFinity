"""Terrain Lock — manual "Conform to Hex" lattice editing for an imported
terrain object.

An earlier revision tried to seat a terrain object onto its hex
automatically: the user picked one anchor point and the code
finite-difference-probed the local slope of both surfaces there, then
applied a single rigid planar tilt via a 2x2x2 lattice. That auto-solve is
gone — real terrain scans aren't reliably a tilted plane around one point —
in favour of direct manual control:

`HEXFINITY_OT_start_conform_lattice` ("Edit Lattice") drops a new Lattice
object around the terrain object (sized to its own local bbox, padded by
`LATTICE_PAD_FACTOR`), binds it with a `LATTICE` modifier, and switches into
Edit Mode on the lattice so the user can Tab in, drag control points by hand,
and watch the mesh deform live — exactly Blender's normal lattice-editing
workflow, with no automatic math involved. The lattice's resolution
(`points_u/v/w`) comes from the terrain object's own
`hexfinity_terrain.lattice_res_u/v/w`, since a 2x2x2 lattice can only ever
produce a whole-object tilt and manual local bending needs more control
points.

`HEXFINITY_OT_apply_conform_lattice` ("Apply") bakes the current lattice
deformation into the mesh (`modifier_apply`), tears the lattice down, and
invalidates + rebuilds the parent tile's plateau cache, mirroring
`operators.HEXFINITY_OT_generate_terrain_plateau`'s own
invalidate-then-rebuild pattern.

`HEXFINITY_OT_cancel_conform_lattice` ("Cancel") discards the edit instead:
it removes the modifier without applying it and tears the lattice down, so
the mesh reverts to exactly its pre-edit shape. No rebuild is needed since
the mesh itself was never touched.

Both Apply and Cancel resolve their target via `_resolve_conform_edit`,
which works whether the *active* object is the terrain mesh or its lattice
— the normal case, since Tabbing into Edit Mode on the lattice makes it the
active object. `panel.py` uses the same resolution (via
`conform_target_for_lattice`) to keep the Apply/Cancel buttons reachable
while a Lattice object is active.
"""

import bpy
from mathutils import Vector, Matrix


LOCK_MODIFIER_NAME = "HF_Conform"
LATTICE_PAD_FACTOR = 1.15      # keep the whole mesh inside the lattice's box


def _conform_modifier(obj):
    """The Conform Lattice modifier on `obj`, or `None`."""
    if obj is None or obj.type != 'MESH':
        return None
    return obj.modifiers.get(LOCK_MODIFIER_NAME)


def conform_target_for_lattice(lat_obj):
    """If `lat_obj` is the live Conform Lattice for its own parent terrain
    object, return that parent; else `None`. Lets `panel.py` show the
    Apply/Cancel controls when the *lattice* — not the mesh — is the active
    object, which is the normal case once the user has Tabbed into Edit
    Mode on it."""
    if lat_obj is None or lat_obj.type != 'LATTICE':
        return None
    parent = lat_obj.parent
    mod = _conform_modifier(parent)
    if mod is not None and mod.object == lat_obj:
        return parent
    return None


def _resolve_conform_edit(context):
    """Resolve `context.active_object` to `(terrain_obj, lattice_obj, tprops)`
    for Apply/Cancel, whether the active object is the terrain mesh or its
    lattice. Self-heals: if `has_conform_lattice` is set but the modifier or
    lattice is missing (e.g. deleted by hand via the Outliner), clears the
    flag and returns `None` rather than leaving a stuck UI state."""
    obj = context.active_object
    if obj is None:
        return None

    if obj.type == 'MESH':
        terrain_obj = obj
    elif obj.type == 'LATTICE':
        terrain_obj = conform_target_for_lattice(obj)
        if terrain_obj is None:
            return None
    else:
        return None

    tprops = terrain_obj.hexfinity_terrain
    mod = _conform_modifier(terrain_obj)
    lat_obj = mod.object if mod is not None else None
    if mod is None or lat_obj is None:
        if tprops.has_conform_lattice:
            tprops.has_conform_lattice = False
        return None

    return terrain_obj, lat_obj, tprops


def _create_manual_lattice(context, model):
    """New LATTICE object sized to `model`'s own local bbox (padded by
    LATTICE_PAD_FACTOR so the whole mesh stays enclosed — Lattice
    deformation is unreliable outside its box), parented to `model` with an
    identity matrix_parent_inverse so lattice points are expressed directly
    in the model's own local axes. Resolution comes from the model's own
    `hexfinity_terrain.lattice_res_u/v/w`. Created at rest — no deformation
    is applied, the user shapes it by hand. Measures `lat_obj.dimensions`
    live right after creation (at scale 1,1,1) rather than assuming a
    constant, since a new lattice's default span isn't something to rely on
    across Blender versions."""
    tprops = model.hexfinity_terrain
    lat_data = bpy.data.lattices.new(f"{model.name}_Conform")
    lat_data.points_u = tprops.lattice_res_u
    lat_data.points_v = tprops.lattice_res_v
    lat_data.points_w = tprops.lattice_res_w
    lat_data.interpolation_type_u = 'KEY_LINEAR'
    lat_data.interpolation_type_v = 'KEY_LINEAR'
    lat_data.interpolation_type_w = 'KEY_LINEAR'
    lat_obj = bpy.data.objects.new(f"{model.name}_Conform", lat_data)

    map_props = context.scene.hexfinity_map
    coll = map_props.root_collection or context.scene.collection
    coll.objects.link(lat_obj)

    lmin = Vector(model.bound_box[0])
    lmax = Vector(model.bound_box[0])
    for c in model.bound_box:
        lmin = Vector((min(lmin[i], c[i]) for i in range(3)))
        lmax = Vector((max(lmax[i], c[i]) for i in range(3)))
    center = (lmin + lmax) * 0.5
    padded = (lmax - lmin) * LATTICE_PAD_FACTOR
    padded = Vector((max(padded[i], 1e-3) for i in range(3)))

    lat_obj.parent = model
    lat_obj.matrix_parent_inverse = Matrix.Identity(4)
    lat_obj.location = center
    base_dim = Vector(lat_obj.dimensions)
    lat_obj.scale = Vector(
        padded[i] / base_dim[i] if base_dim[i] > 1e-9 else 1.0
        for i in range(3))
    return lat_obj


def _remove_lattice(lat_obj):
    lat_data = lat_obj.data
    bpy.data.objects.remove(lat_obj, do_unlink=True)
    if lat_data is not None and lat_data.users == 0:
        bpy.data.lattices.remove(lat_data)


class HEXFINITY_OT_start_conform_lattice(bpy.types.Operator):
    bl_idname = "hexfinity.start_conform_lattice"
    bl_label = "Edit Lattice"
    bl_description = ("Add a Lattice around this terrain object and enter "
                      "Edit Mode on it — drag its control points to bend "
                      "the mesh by hand, then Apply to bake the result or "
                      "Cancel to discard it.")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        from . import operators
        obj = context.active_object
        return (context.scene.hexfinity_map.is_generated and obj is not None
                and operators._is_terrain_object(obj)
                and not obj.hexfinity_terrain.has_conform_lattice)

    def execute(self, context):
        obj = context.active_object
        tprops = obj.hexfinity_terrain

        lat_obj = _create_manual_lattice(context, obj)
        mod = obj.modifiers.new(name=LOCK_MODIFIER_NAME, type='LATTICE')
        mod.object = lat_obj
        tprops.has_conform_lattice = True

        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        lat_obj.select_set(True)
        context.view_layer.objects.active = lat_obj
        bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, "Drag lattice points, then Apply or Cancel.")
        return {'FINISHED'}


class HEXFINITY_OT_apply_conform_lattice(bpy.types.Operator):
    bl_idname = "hexfinity.apply_conform_lattice"
    bl_label = "Apply"
    bl_description = ("Bake the current lattice deformation into the "
                      "terrain mesh, remove the lattice, and regenerate "
                      "the tile's plateau.")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        from . import operators
        resolved = _resolve_conform_edit(context)
        if resolved is None:
            return False
        terrain_obj, _lat_obj, tprops = resolved
        if not tprops.has_conform_lattice:
            return False
        return operators._terrain_object_within_own_hex(
            context.scene.hexfinity_map, terrain_obj)

    def execute(self, context):
        from . import operators
        resolved = _resolve_conform_edit(context)
        if resolved is None:
            self.report({'ERROR'}, "No active Conform Lattice found.")
            return {'CANCELLED'}
        obj, lat_obj, tprops = resolved
        tile = obj.parent

        # `obj` (the mesh) is never itself put into Edit Mode — it's the
        # lattice that is, once the user Tabs in — so checking obj.mode here
        # would always read 'OBJECT' and skip the exit entirely. The global
        # interaction mode (context.mode) is what modifier_apply's poll
        # actually cares about.
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        mod = _conform_modifier(obj)
        with context.temp_override(active_object=obj,
                                   selected_objects=[obj], object=obj):
            bpy.ops.object.modifier_apply(modifier=mod.name)

        _remove_lattice(lat_obj)
        tprops.has_conform_lattice = False

        if tile is not None:
            for key in ("hf_terrain_pads", "hf_terrain_pads_sig"):
                if tile.get(key) is not None:
                    del tile[key]
            operators.rebuild_tile(tile)

        context.view_layer.objects.active = obj
        obj.select_set(True)

        self.report({'INFO'}, f"Applied lattice conform to {obj.name}.")
        return {'FINISHED'}


class HEXFINITY_OT_cancel_conform_lattice(bpy.types.Operator):
    bl_idname = "hexfinity.cancel_conform_lattice"
    bl_label = "Cancel"
    bl_description = ("Discard the current lattice edit and remove it, "
                      "reverting the terrain mesh to its pre-edit shape.")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        resolved = _resolve_conform_edit(context)
        return resolved is not None and resolved[2].has_conform_lattice

    def execute(self, context):
        resolved = _resolve_conform_edit(context)
        if resolved is None:
            self.report({'ERROR'}, "No active Conform Lattice found.")
            return {'CANCELLED'}
        obj, lat_obj, tprops = resolved

        # Same reasoning as Apply: the lattice, not obj, is what's in Edit
        # Mode, so the check has to be against the global mode.
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        mod = _conform_modifier(obj)
        if mod is not None:
            obj.modifiers.remove(mod)
        _remove_lattice(lat_obj)
        tprops.has_conform_lattice = False

        context.view_layer.objects.active = obj
        obj.select_set(True)

        self.report({'INFO'}, f"Cancelled lattice edit on {obj.name}.")
        return {'FINISHED'}
