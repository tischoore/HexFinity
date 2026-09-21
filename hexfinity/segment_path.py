"""Draw Segments Path — the consumer of settings.json's segment_path
library (see segments.py/segment_geometry.py/segment_settings.py, the
"Add Path Segment Type" authoring tool). Places pre-built segment STLs
(bridges, junctions, etc.) across the generated hex tiles it touches,
snapping connector waypoint to connector waypoint, and boolean-clipping a
piece that physically straddles a hex boundary into per-hex pieces — the
same INTERSECT-against-a-hex-prism pattern operators._cut_terrain_by_hex
already uses to split an overhanging terrain object. No pre-selection is
required: a piece that straddles into any generated neighbour is clipped
and the chain continues onto it automatically, the same "always continue
onto a generated neighbour" model path_features.py uses. Right-click/Esc
remain the only way to end a run.

Unlike a regular Path Feature (an open polyline carved as a heightmap
groove into the tile's own generated mesh, see path_features.py), a placed
segment is a real, separate mesh Object, parented under its tile exactly
like a terrain object or a scatter boulder — never fused into
mesh_builder.build_hex_tile's own manifold-checked pipeline. Each hex's
piece is still recorded as an entry in that tile's `path_features`
CollectionProperty (`feature_type == 'SEGMENT'`) purely so the existing
list UI / Remove button work unchanged; `path_features.path_specs()`
never sees these entries carve anything, since a SEGMENT feature's
`points` collection is always left empty.

A placed piece is a single rigid body — its own mesh geometry is never
touched, only its position/rotation — but it is not purely horizontal:
`_fit_placement` tilts it (pitch/roll, on top of the user's own yaw) so
it *rests* on the hovered tile's live surface under its authored Corners
footprint (see segments.py's "Define Corners" workflow, `corners_local_mm`
in settings.json), the same way a rigid flat object settles onto an
uneven floor — touching at whichever corners are locally highest
(generically 3 of them; 3 points always determine a plane) and never
sinking below the surface anywhere, via `segment_geometry.fit_resting_plane`.
A 4+ corner footprint over genuinely non-planar (hilly/saddle) terrain
will generically leave one or more corners floating a small, minimized
gap above the surface rather than touching it too — a rigid body only has
enough freedom to satisfy 3 independent height constraints at once, the
same reason a 4-legged table wobbles on an uneven floor — but critically,
no corner is ever placed *below* the surface (visibly buried into the
terrain), unlike a plain least-squares fit. `fit_resting_plane` itself
solves a *linear* plane model, though, and the rotation actually applied
is a true 3D rotation, which — for a non-trivial tilt — shifts each
corner's real world X/Y slightly off the flat position the fit's own
samples were taken at (a second-order effect the linear model can't see);
`_fit_placement` catches this with one more raycast per corner at each
corner's *true* final position, and lifts the whole piece straight up
(tilt untouched) by whatever tiny amount clears the worst case if any
corner would otherwise still end up sinking in — so the "never below the
surface" guarantee holds for the real applied geometry, not just the
linear model it was derived from. That floating gap is accepted as-is,
not papered over with a mesh deformation: a placed segment is meant to
stay exactly the rigid prefab it was authored as, so it always
prints/exports as the same solid piece regardless of where it was placed.
If a specific placement needs its corners to match the surface exactly,
that's a separate, explicit, user-driven step — terrain_lock.py's existing
"Edit Lattice" Conform workflow already does precisely this (bend an
object's own mesh by hand via a Lattice modifier to match its hex's
generated surface); it currently only targets true terrain objects
(`operators._is_terrain_object` excludes a `SEGMENT_PIECE_TAG`-tagged
piece), so using it on a placed segment is a possible future extension,
not something this module does on its own. When the piece is chaining
onto a previous piece (or a hex edge) via a snapped connector, the
resting fit is additionally pinned to pass exactly through that
connector's target Z (even if that means a slightly larger floating gap
elsewhere, and skipping the lift-correction above entirely, since a
uniform lift would break that exact alignment), so a chained joint never
gets a gap. A segment authored before this feature existed (no
`corners_local_mm` in settings.json) or with fewer than 3 corners falls
back to the old flat/yaw-only placement.

This is a deliberately narrow generalization of the old fully-rigid model,
not the free-form auto-tilt-to-terrain terrain_lock.py already tried and
abandoned as unreliable for a whole terrain object: the fit here is driven
entirely by the Corners polygon the user explicitly authored, never solved
from scratch off a single picked anchor.

Performance: the live hover/ghost pipeline (everything `_update_hover`
does on `MOUSEMOVE`) is throttled to `HOVER_UPDATE_INTERVAL_S` (~30/sec)
rather than recomputed on every single event, shares one
`evaluated_depsgraph_get()` across all of a single update's raycasts, and
caches the hovered tile's own hex-edge snap points
(`_tile_edge_snap_targets`) across mouse-moves that stay over the same
tile -- the same "only re-extract when the hovered tile changes" idiom
`regions.py`'s flood-fill modal already uses. The ghost itself
(`_draw`/`_local_triangles`) never touches the segment's real (often very
high-poly) imported STL geometry at all -- it renders a small prism built
from the segment's own authored convex hull (`hull_local_mm`, captured
once at authoring time) instead, via `segment_geometry.hull_prism_triangles`,
so drag responsiveness no longer scales with how detailed the underlying
model is. The final committed piece always uses the real geometry
regardless, unmodified.
"""

import math
import os
import time
import uuid

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector, Matrix

from .map import (edge_snap_points, corner_xy, EDGE_DIRECTIONS,
                  neighbour_coord, hex_prism_verts_faces, point_in_hex)
from . import segment_geometry


SNAP_RADIUS_PX = 26.0
# A segment's own connector waypoints move/rotate with the piece as the user
# drags/scrolls, so landing one within SNAP_RADIUS_PX of a target is a
# harder two-point aiming task than path_features.py's plain
# cursor-to-target snap -- hence a wider radius here than that module's own
# (separate) SNAP_RADIUS_PX = 18.0.
#
# Continuing a chain requires snapping onto one of the still-open
# connectors left by the piece(s) placed so far (self._open_connectors,
# enforced after the fact by _connects_to_prev) -- a piece can leave more
# than one end open (the very first piece of a run, before either end is
# consumed; or a 3+-way junction piece with only one end consumed), and any
# of them is a valid continuation, so all of them get this even more
# generous, prioritized catch radius. Any other candidate within it is
# irrelevant anyway, since only an open connector can ever result in a
# valid placement while a chain is in progress.
REQUIRED_CONNECTOR_SNAP_RADIUS_PX = 42.0
# A floor on real-world catch distance, so the effective snap tolerance
# never shrinks below this many mm no matter how far the view is zoomed in
# -- SNAP_RADIUS_PX/REQUIRED_CONNECTOR_SNAP_RADIUS_PX are pure screen-pixel
# radii, so at high zoom (likely while fitting a piece precisely) their
# real-world equivalent otherwise keeps shrinking. See
# _snap_effective_dist.
MIN_SNAP_WORLD_MM = 2.0
ROTATION_STEP_DEG = 15.0
VIEW_PAN_DURATION_S = 1.2
# Caps the pitch/roll _fit_placement derives from a segment's authored
# Corners -- a safety valve against one bad/missed surface raycast sample
# (e.g. a corner that lands past the tile's own rim before boolean
# clipping) producing a wildly tilted placement.
MAX_TILT_DEG = 30.0
# Generous vs. operators.SPLIT_PRISM_MARGIN_MM (5 mm) -- a bridge/junction
# segment can rise well above a flat terrain object's own bbox, and the
# boolean tool prism just needs to fully contain the piece being clipped.
SPLIT_PRISM_MARGIN_MM = 50.0
# Caps how often a MOUSEMOVE event actually triggers a hover recompute
# (raycasts + tilt fit + ghost rebuild) -- decouples that cost from
# however fast the OS/input device delivers MOUSEMOVE events, which can
# be far higher than this. ~30/sec is smooth enough for a drag preview
# without redoing the full hover pipeline on every single event.
HOVER_UPDATE_INTERVAL_S = 1.0 / 30.0

_GHOST_COLOR = (0.3, 0.7, 1.0, 0.35)
_GHOST_SNAP_COLOR = (0.35, 1.0, 0.55, 0.55)
# Every available-but-unsnapped snap target (hex-edge points + this run's
# open connectors), drawn dim and small so the user can see at a glance
# where there is something to aim for, before they've gotten close to any
# of it.
_SNAP_CANDIDATE_COLOR = (1.0, 1.0, 1.0, 0.35)
# The one target currently engaged (self._snap_world) -- drawn bright and
# larger, on top of the candidate dots, so the exact point a connector will
# land on is unambiguous. Reuses _GHOST_SNAP_COLOR's hue for consistency.
_SNAP_ACTIVE_COLOR = (0.35, 1.0, 0.55, 1.0)

# Custom id-properties stamped on every placed/clipped piece Object -- the
# "state lives on the real object" convention segments.py's own docstring
# already calls out for terrain_lock.py, reused here instead of more RNA on
# HexFinityPathFeature (which only gets segment_run_id/segment_piece).
SEGMENT_PIECE_TAG = "hf_segment_piece"
SEGMENT_TYPE_NAME = "hf_segment_type_name"
SEGMENT_SOURCE_FILE = "hf_segment_source_file"

_ACTIVE = False


def is_active():
    return _ACTIVE


# ---------------------------------------------------------------------------
# settings.json access + mesh import/cache -- mirrors flora.py's
# _get_or_import_mesh shape, keyed by the segment's own absolute filepath
# (an externally-referenced asset, not a bundled per-species folder).

def _load_types():
    from . import segments as seg_authoring
    data = seg_authoring._load_settings()
    return data["segment_path"]["types"]


_mesh_cache = {}   # filepath -> bpy.types.Mesh (shared, use_fake_user=True)


def _get_or_import_segment_mesh(filepath):
    mesh = _mesh_cache.get(filepath)
    if mesh is not None:
        # An undo/redo elsewhere in the session can swap out Blender's
        # entire bpy.data state, leaving this cached reference pointing at
        # a freed ID -- accessing *any* attribute on it then raises
        # ReferenceError rather than behaving like a normal stale lookup,
        # so the validity check itself must be guarded.
        try:
            still_valid = mesh.name in bpy.data.meshes
        except ReferenceError:
            still_valid = False
        if still_valid:
            return mesh
        del _mesh_cache[filepath]

    if not hasattr(bpy.ops.wm, "stl_import") or not os.path.isfile(filepath):
        return None
    scene = bpy.context.scene
    before = set(scene.objects)
    try:
        bpy.ops.wm.stl_import(filepath=filepath)
    except RuntimeError:
        return None
    imported = [o for o in scene.objects if o not in before]
    if not imported:
        return None

    mesh = imported[0].data
    mesh.name = f"HF_Segment_{os.path.splitext(os.path.basename(filepath))[0]}"
    mesh.use_fake_user = True
    bpy.data.objects.remove(imported[0], do_unlink=True)
    for extra in imported[1:]:
        bpy.data.objects.remove(extra, do_unlink=True)

    _mesh_cache[filepath] = mesh
    return mesh


def _snap_effective_dist(pixel_dist, world_dist, radius_px, min_world_mm):
    """None if a snap candidate doesn't qualify at all; otherwise a
    pixel-space distance usable for ranking it against other candidates.

    A candidate whose real-world distance is within `min_world_mm` always
    qualifies -- clamped to `radius_px` so it can never out-rank a
    genuinely pixel-closer candidate -- even when its projected pixel
    distance exceeds `radius_px`. That's what keeps the catch zone from
    shrinking to an impractically small real-world size the further the
    view is zoomed in (see MIN_SNAP_WORLD_MM's own comment). Otherwise,
    ordinary screen-pixel-radius qualification applies. Plain floats in,
    float-or-None out -- no bpy/3D-viewport dependency, so this is
    directly unit-testable."""
    if world_dist <= min_world_mm:
        return min(pixel_dist, radius_px)
    if pixel_dist <= radius_px:
        return pixel_dist
    return None


def _connector_locals(seg):
    """[(x_mm, y_mm), ...] of `seg`'s edge-tagged (edge_idx >= 0) waypoints
    -- its connection points, in the segment's own local mm space."""
    return [(wp["x_mm"], wp["y_mm"]) for wp in seg["waypoints"] if wp["edge_idx"] >= 0]


def _corners_local(seg):
    """[(x_mm, y_mm), ...] of `seg`'s authored Corners footprint (see
    segments.py's "Define Corners" workflow), or [] for a segment entry
    written before that feature existed -- settings.json's
    corners_local_mm is a plain list of [x, y] pairs (segment_settings.
    add_segment's own docstring: "a future reader must use
    .get('corners_local_mm', [])"). _fit_placement falls back to a flat
    placement whenever this has fewer than 3 points."""
    return [(x, y) for (x, y) in seg.get("corners_local_mm", [])]


# ---------------------------------------------------------------------------
# Entry point: a small popup (Cancel / Draw) offering every type currently
# in settings.json.

def _type_items(self, context):
    types = _load_types()
    if not types:
        return [('NONE', "No segment types in settings.json", "")]
    return [(name, name, "") for name in sorted(types.keys())]


class HEXFINITY_OT_cancel_segments_path_dialog(bpy.types.Operator):
    """Inert Cancel button for the Draw Segments Path popup -- mirrors
    segments.HEXFINITY_OT_segment_type_info's "does nothing, just closes
    the popup" convention."""
    bl_idname = "hexfinity.cancel_segments_path_dialog"
    bl_label = "Cancel"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        return {'CANCELLED'}


class HEXFINITY_OT_draw_segments_path_dialog(bpy.types.Operator):
    bl_idname = "hexfinity.draw_segments_path_dialog"
    bl_label = "Draw Segments Path"
    bl_description = ("Pick a segment type, then click-place a chain of its "
                      "segments across the generated hexes")
    bl_options = {'INTERNAL'}

    type_name: bpy.props.EnumProperty(name="Type", items=_type_items)

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    def invoke(self, context, event):
        if not _load_types():
            self.report({'WARNING'},
                        "No segment types in settings.json -- use Add Path "
                        "Segment Type (Settings box) first.")
            return {'CANCELLED'}
        return context.window_manager.invoke_popup(self, width=260)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "type_name", text="")
        row = layout.row(align=True)
        row.operator("hexfinity.cancel_segments_path_dialog", text="Cancel")
        draw_op = row.operator("hexfinity.start_segments_path_draw", text="Draw")
        draw_op.type_name = self.type_name

    def execute(self, context):
        # invoke_popup draws no OK/Cancel footer of its own -- the two
        # explicit buttons in draw() are the real interaction. This
        # execute() only exists because Blender requires one on every
        # Operator; it's never reached via the popup's own buttons.
        return {'CANCELLED'}


# ---------------------------------------------------------------------------
# The modal placement operator.

class HEXFINITY_OT_start_segments_path_draw(bpy.types.Operator):
    bl_idname = "hexfinity.start_segments_path_draw"
    bl_label = "Draw Segments Path"
    bl_description = "Click-place segments of the chosen type across the generated hexes"
    bl_options = {'REGISTER'}

    type_name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.scene.hexfinity_map.is_generated

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Draw Segments Path must be started in the 3D viewport")
            return {'CANCELLED'}
        types = _load_types()
        segments = types.get(self.type_name, {}).get("segments", [])
        if not segments:
            self.report({'ERROR'}, f"No segments registered under type {self.type_name!r}.")
            return {'CANCELLED'}

        self._segments = segments
        self._variant_index = 0
        self._rotation_z = 0.0
        self._run_id = uuid.uuid4().hex
        self._cursor = (event.mouse_region_x, event.mouse_region_y)

        self._hover_tile = None
        self._hover_world = None
        self._transform = None          # (translation Vector, rotation Matrix 3x3) or None
        self._snap_world = None
        self._snap_meta = None
        self._hover_targets = []        # [(Vector, meta), ...] -- every snap candidate this hover
        self._open_connectors = []      # [{"world": Vector, "tile": Object}, ...] -- every
                                        # still-open end of the running chain; empty means the
                                        # very next piece placed needs no connection (first of the run)

        self._ghost_tri_cache = {}      # filepath -> [(v0, v1, v2), ...] local verts
        self._edge_snap_cache_tile = None
        self._edge_snap_cache_targets = []
        self._last_hover_update_time = 0.0
        self._pan_timer = None
        self._pan_start = None
        self._pan_target = None
        self._pan_start_time = 0.0

        global _ACTIVE
        _ACTIVE = True

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), 'WINDOW', 'POST_VIEW')
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Draw Segments Path:  LMB = place    Scroll = rotate    "
            "+/- = next/prev segment    RMB/Esc = finish")
        self._update_hover(context, event)
        if context.area is not None:
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    # -- modal event loop --------------------------------------------------

    def modal(self, context, event):
        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._cursor = (event.mouse_region_x, event.mouse_region_y)
            now = time.monotonic()
            if now - self._last_hover_update_time >= HOVER_UPDATE_INTERVAL_S:
                self._last_hover_update_time = now
                self._update_hover(context, event)
                self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'WHEELUPMOUSE' and event.value == 'PRESS':
            self._rotation_z += math.radians(ROTATION_STEP_DEG)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'WHEELDOWNMOUSE' and event.value == 'PRESS':
            self._rotation_z -= math.radians(ROTATION_STEP_DEG)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type in {'NUMPAD_PLUS', 'EQUAL'} and event.value == 'PRESS':
            self._variant_index = (self._variant_index + 1) % len(self._segments)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type in {'NUMPAD_MINUS', 'MINUS'} and event.value == 'PRESS':
            self._variant_index = (self._variant_index - 1) % len(self._segments)
            self._update_hover(context, event)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if context.region is None or context.region.type != 'WINDOW':
                return {'PASS_THROUGH'}
            self._try_place(context)
            self._tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._finish(context)
            return {'FINISHED'}

        if event.type == 'TIMER':
            self._advance_view_pan(context)
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}

    @staticmethod
    def _tag_redraw(context):
        if context.area is not None:
            context.area.tag_redraw()

    # -- hover / snapping ----------------------------------------------------

    def _current_segment(self):
        return self._segments[self._variant_index]

    @staticmethod
    def _generated_tiles(context):
        """Every generated tile in the map -- the candidate set for hover
        raycasting and boolean-clip splitting, mirroring the plain
        `is_generated`-filtered scan operators.py's own export step uses.
        No pre-selection is required; a piece straddling into any of these
        is clipped and the run continues onto it automatically."""
        map_props = context.scene.hexfinity_map
        coll = map_props.root_collection
        if coll is None:
            return []
        return [o for o in coll.objects if o.hexfinity_tile.is_generated]

    def _raycast_generated_tile(self, context, coord, depsgraph=None):
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return None, None
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        if depsgraph is None:
            depsgraph = context.evaluated_depsgraph_get()
        hit, location, _n, _i, hit_obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction)
        if not hit or hit_obj is None:
            return None, None
        tile = hit_obj.original
        if not tile.hexfinity_tile.is_generated:
            return None, None
        return tile, location.copy()

    @staticmethod
    def _surface_z_at(context, tile, x, y, depsgraph=None):
        """Straight-down raycast onto `tile`'s own current surface at world
        (x, y) -- unlike path_features.py's floating "man height above the
        hex" draw plane, a segment is a physical object that must sit ON
        the tile, so every snap target needs a real surface Z, not a
        constant offset one. `depsgraph`, when given, is reused as-is
        instead of fetched fresh -- callers doing several of these per
        hover update (`_tile_edge_snap_targets`, `_fit_placement`) share
        one `context.evaluated_depsgraph_get()` rather than paying for it
        again on every single raycast."""
        if depsgraph is None:
            depsgraph = context.evaluated_depsgraph_get()
        origin = Vector((x, y, tile.matrix_world.translation.z + 100000.0))
        direction = Vector((0.0, 0.0, -1.0))
        hit, location, _n, _i, hit_obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction)
        if hit and hit_obj is not None and hit_obj.original == tile:
            return location.z
        return tile.matrix_world.translation.z

    def _tile_edge_snap_targets(self, context, tile, depsgraph=None):
        """[(Vector, ('edge', edge_idx, tile)), ...] for `tile`'s own
        hex-edge points, Z resolved on the real surface.

        These points are intrinsic to `tile` itself, not to wherever the
        piece being placed currently is -- so, mirroring
        regions.HEXFINITY_OT_flood_fill_region._update_hover's own "only
        re-extract when the hovered tile changes" idiom, they're cached
        per tile (`self._edge_snap_cache_tile`/`_targets`) and only
        recomputed (12 raycasts, at the default edge_snap=3) when the
        hovered tile actually changes, instead of on every hover update
        for however long the mouse stays over the same tile."""
        if tile is self._edge_snap_cache_tile:
            return self._edge_snap_cache_targets

        map_props = context.scene.hexfinity_map
        mw = tile.matrix_world
        edge_snap = 3
        n = max(2, edge_snap)
        per_edge = n - 1
        out = []
        for i, (x, y) in enumerate(edge_snap_points(map_props.diameter_mm, edge_snap)):
            world_xy = mw @ Vector((x, y, 0.0))
            z = self._surface_z_at(context, tile, world_xy.x, world_xy.y, depsgraph)
            out.append((Vector((world_xy.x, world_xy.y, z)), ('edge', i, tile)))

        self._edge_snap_cache_tile = tile
        self._edge_snap_cache_targets = out
        return out

    def _snap_targets(self, context, tile, depsgraph=None):
        targets = []
        if tile is not None:
            targets.extend(self._tile_edge_snap_targets(context, tile, depsgraph))
        for oc in self._open_connectors:
            targets.append((oc["world"], ('open', oc)))
        return targets

    def _fit_placement(self, context, tile, seg, translation, yaw,
                        anchor_local, anchor_world_z, depsgraph=None):
        """Returns (translation: Vector, rotation: Matrix 3x3): `seg`
        placed at `translation`'s X/Y with yaw `yaw`, tilted (see the
        module docstring) as a single rigid body so it *rests* on
        `tile`'s live surface -- touching at whichever authored Corners
        are locally highest, any others left floating a small, minimized
        gap above the surface, but never sinking below it anywhere --
        pinned exactly through anchor_local/anchor_world_z (a snapped
        connector's local X/Y and target world Z) when given.

        A rigid tilt can only ever touch at most 3 independent corners
        exactly (see fit_resting_plane's own docstring for why); a 4+
        corner footprint over non-planar (hilly/saddle) terrain generically
        leaves one or more corners floating above the surface rather than
        touching it. That's accepted as-is: the segment's own mesh is
        never deformed to chase an exact fit for every corner (see the
        module docstring for why) -- but unlike a plain least-squares fit,
        no corner is ever placed *below* the surface (visibly buried into
        the terrain).

        Falls back to a flat, yaw-only placement at `translation`'s own Z
        (the flat raycast/snap fallback the caller already resolved) when
        `tile` is None, `seg` has fewer than 3 authored corners, or no
        resting (or, failing that, least-squares) fit can be found at all
        (e.g. every corner collinear in X/Y)."""
        yaw_matrix = Matrix.Rotation(yaw, 3, 'Z')
        flat = (translation.copy(), yaw_matrix)

        corners = _corners_local(seg)
        if tile is None or len(corners) < 3:
            return flat

        samples = []
        for (x, y) in corners:
            rotated_xy = yaw_matrix @ Vector((x, y, 0.0))
            wx = translation.x + rotated_xy.x
            wy = translation.y + rotated_xy.y
            samples.append((x, y, self._surface_z_at(context, tile, wx, wy, depsgraph)))

        fit = segment_geometry.fit_resting_plane(samples, anchor_local, anchor_world_z)
        if fit is None:
            # Fully degenerate resting-plane search (e.g. every corner
            # collinear) -- fall back to the least-squares fit rather than
            # giving up on tilting altogether.
            fit = segment_geometry.fit_tilt_plane(samples, anchor_local, anchor_world_z)
        if fit is None:
            return flat

        pivot_x, pivot_y, pivot_z, slope_x, slope_y = fit
        slope_x, slope_y = segment_geometry.clamp_tilt_slopes(
            slope_x, slope_y, MAX_TILT_DEG)

        # Minimal-angle rotation taking the piece's own local +Z onto the
        # fitted plane's normal, applied in local space *before* yaw --
        # yaw only ever rotates around world Z, so it never changes a
        # vector's Z component, meaning this tilt (solved against the
        # un-yawed local corners) stays valid however self._rotation_z
        # is currently set.
        normal_local = Vector((-slope_x, -slope_y, 1.0)).normalized()
        tilt_matrix = Vector((0.0, 0.0, 1.0)).rotation_difference(normal_local).to_matrix()
        rotation = yaw_matrix @ tilt_matrix

        pivot_after_tilt = tilt_matrix @ Vector((pivot_x, pivot_y, 0.0))
        origin_z = pivot_z - pivot_after_tilt.z
        result_translation = Vector((translation.x, translation.y, origin_z))

        if anchor_local is None:
            # fit_resting_plane solves a *linear* plane model against
            # samples taken at each corner's flat (pre-tilt) world X/Y.
            # The rotation actually applied is a true 3D rotation, though,
            # which -- for a non-trivial tilt -- shifts each corner's real
            # world X/Y slightly off that flat position too (not just its
            # Z), a second-order effect the linear model can't see. For a
            # steep enough tilt this can leave a corner just barely
            # sinking below the *real* surface at its own true final
            # position, even though the linear fit guaranteed it wouldn't.
            # One more raycast per corner, at each corner's true final
            # world X/Y, catches this; if any corner still comes out
            # below the surface there, the whole piece is lifted straight
            # up (tilt/rotation untouched) by whatever tiny amount clears
            # the worst case, so no corner ever visibly penetrates the
            # terrain. Skipped when pinned to a connector (anchor_local is
            # not None) -- a uniform lift would break that exact joint
            # alignment, which takes priority there over this safety
            # margin.
            worst = 0.0
            for (cx, cy, _) in samples:
                w = rotation @ Vector((cx, cy, 0.0))
                wx = result_translation.x + w.x
                wy = result_translation.y + w.y
                wz = result_translation.z + w.z
                real_z = self._surface_z_at(context, tile, wx, wy, depsgraph)
                worst = min(worst, wz - real_z)
            if worst < 0.0:
                result_translation.z += -worst + 1e-4

        return result_translation, rotation

    def _update_hover(self, context, event):
        coord = (event.mouse_region_x, event.mouse_region_y)
        # One depsgraph fetch shared by every raycast this hover update
        # needs (the hover pick, the tile's own edge points when its
        # cache misses, and each authored-corner sample) instead of each
        # of those fetching its own.
        depsgraph = context.evaluated_depsgraph_get()
        tile, hit_world = self._raycast_generated_tile(context, coord, depsgraph)
        self._hover_tile = tile
        self._hover_world = hit_world
        self._snap_world = None
        self._snap_meta = None
        self._transform = None
        self._hover_targets = []
        if hit_world is None:
            return

        seg = self._current_segment()
        connectors = _connector_locals(seg)
        cos_a, sin_a = math.cos(self._rotation_z), math.sin(self._rotation_z)

        def rotated(local_xy):
            x, y = local_xy
            return Vector((x * cos_a - y * sin_a, x * sin_a + y * cos_a, 0.0))

        translation = hit_world.copy()

        region = context.region
        rv3d = context.region_data
        targets = self._snap_targets(context, tile, depsgraph)
        # Stashed so _draw can render every available target without
        # recomputing this list itself.
        self._hover_targets = targets

        def _search(candidates, radius_px):
            """Nearest-by-_snap_effective_dist candidate among `candidates`,
            searched against every one of the segment's own connector
            waypoints at the current hover translation/rotation. Returns
            (local_xy, target_world, meta) or None."""
            best = None
            best_dist = radius_px
            for local_xy in connectors:
                world_guess = translation + rotated(local_xy)
                s_guess = view3d_utils.location_3d_to_region_2d(region, rv3d, world_guess)
                if s_guess is None:
                    continue
                for target_world, meta in candidates:
                    s_target = view3d_utils.location_3d_to_region_2d(region, rv3d, target_world)
                    if s_target is None:
                        continue
                    pixel_dist = math.hypot(s_target.x - s_guess.x, s_target.y - s_guess.y)
                    world_dist = (target_world - world_guess).length
                    eff = _snap_effective_dist(pixel_dist, world_dist, radius_px, MIN_SNAP_WORLD_MM)
                    if eff is not None and eff <= best_dist:
                        best_dist = eff
                        best = (local_xy, target_world, meta)
            return best

        best = None
        if self._open_connectors:
            # Every still-open connector of the piece(s) placed so far --
            # not just one -- is a valid target for continuing the chain
            # (the piece just placed may have left more than one end open,
            # e.g. the very first piece of a run with both ends free, or a
            # 3+-way junction piece with only one end consumed). Any of
            # them gets this wider, prioritized catch radius; anything else
            # within the ordinary radius is irrelevant while a chain is in
            # progress, since _connects_to_prev only ever accepts a snap
            # onto one of these.
            required_candidates = [(oc["world"], ('open', oc))
                                    for oc in self._open_connectors]
            best = _search(required_candidates, REQUIRED_CONNECTOR_SNAP_RADIUS_PX)
        if best is None:
            best = _search(targets, SNAP_RADIUS_PX)

        anchor_local, anchor_world_z = None, None
        if best is not None:
            local_xy, target_world, meta = best
            translation = target_world - rotated(local_xy)
            self._snap_world = target_world
            self._snap_meta = meta
            anchor_local, anchor_world_z = local_xy, target_world.z

        self._transform = self._fit_placement(
            context, tile, seg, translation, self._rotation_z,
            anchor_local, anchor_world_z, depsgraph)

    def _connects_to_prev(self):
        if not self._open_connectors:
            return True  # the very first piece of the run needs no connection
        if self._snap_meta is None or self._snap_meta[0] != 'open':
            return False
        oc = self._snap_meta[1]
        # oc is the exact dict object _snap_targets/_update_hover pulled
        # out of self._open_connectors -- identity check, not a
        # recomputed-distance one, since it's the same object either way.
        return any(oc is cand for cand in self._open_connectors)

    # -- placement -----------------------------------------------------------

    def _try_place(self, context):
        if self._transform is None:
            self.report({'INFO'}, "Hover over a generated hex to place")
            return
        if not self._connects_to_prev():
            self.report({'WARNING'},
                        "Snap a connector waypoint to the previous segment's "
                        "open end before placing")
            return
        seg = self._current_segment()
        translation, rotation = self._transform
        self._commit_piece(context, seg, translation, rotation)

    @staticmethod
    def _world_bbox(obj):
        pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        zs = [p.z for p in pts]
        return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))

    def _clip_to_hexes(self, context, obj, map_props):
        """Boolean-INTERSECT `obj` against every generated tile's hex prism
        it overlaps (bbox pre-filtered, mirrors operators._hex_split_candidates),
        exactly like operators._cut_terrain_by_hex splits an overhanging
        terrain object. Returns [(tile, piece_obj), ...]; `obj` itself is
        reused directly (no boolean) when only one hex is involved, or
        consumed (removed) once its clipped pieces exist."""
        bmin, bmax = self._world_bbox(obj)
        half = map_props.diameter_mm * 0.5
        candidates = []
        for tile in self._generated_tiles(context):
            tx, ty = tile.location.x, tile.location.y
            if (bmax.x < tx - half or bmin.x > tx + half
                    or bmax.y < ty - half or bmin.y > ty + half):
                continue
            candidates.append(tile)
        if not candidates:
            return []
        if len(candidates) == 1:
            return [(candidates[0], obj)]

        coll = map_props.root_collection
        z_min = bmin.z - SPLIT_PRISM_MARGIN_MM
        z_max = bmax.z + SPLIT_PRISM_MARGIN_MM
        pieces = []
        for tile in candidates:
            tx, ty = tile.location.x, tile.location.y
            verts, faces = hex_prism_verts_faces(map_props.diameter_mm, z_min, z_max)
            verts = [(x + tx, y + ty, z) for x, y, z in verts]
            prism_mesh = bpy.data.meshes.new("hf_segment_prism_tmp")
            prism_mesh.from_pydata(verts, [], faces)
            prism_mesh.update(calc_edges=True)
            prism_obj = bpy.data.objects.new("hf_segment_prism_tmp", prism_mesh)
            coll.objects.link(prism_obj)

            dup_mesh = obj.data.copy()
            dup_obj = bpy.data.objects.new(f"HF_SegmentPiece_{tile.name}", dup_mesh)
            coll.objects.link(dup_obj)
            dup_obj.matrix_world = obj.matrix_world.copy()

            mod = dup_obj.modifiers.new(name="hf_segment_cut", type='BOOLEAN')
            mod.operation = 'INTERSECT'
            mod.solver = 'EXACT'
            mod.object = prism_obj
            with context.temp_override(active_object=dup_obj,
                                       selected_objects=[dup_obj],
                                       object=dup_obj):
                bpy.ops.object.modifier_apply(modifier=mod.name)

            bpy.data.objects.remove(prism_obj, do_unlink=True)
            bpy.data.meshes.remove(prism_mesh)

            if len(dup_obj.data.polygons) == 0:
                bpy.data.objects.remove(dup_obj, do_unlink=True)
                bpy.data.meshes.remove(dup_mesh)
                continue
            pieces.append((tile, dup_obj))

        # Only consume `obj` once it actually produced at least one non-empty
        # piece — a bbox pre-filter can admit a candidate tile whose real
        # (non-axis-aligned) hex prism the piece never actually overlaps, so
        # every intersection can come out empty even with >1 candidate. In
        # that case `obj` must survive for the caller's own "no pieces"
        # cleanup (_commit_piece) — removing it unconditionally here left
        # that cleanup trying to remove an already-freed object.
        if pieces:
            bpy.data.objects.remove(obj, do_unlink=True)
        return pieces

    @staticmethod
    def _shared_edge_idx(tile_a, tile_b):
        qa, ra = tile_a.hexfinity_tile.coord_q, tile_a.hexfinity_tile.coord_r
        qb, rb = tile_b.hexfinity_tile.coord_q, tile_b.hexfinity_tile.coord_r
        for i, d in enumerate(EDGE_DIRECTIONS):
            if neighbour_coord(qa, ra, d) == (qb, rb):
                return i
        return None

    def _edge_endpoints_world(self, context, tile, edge_idx):
        map_props = context.scene.hexfinity_map
        mw = tile.matrix_world
        a = corner_xy(edge_idx, map_props.diameter_mm)
        b = corner_xy((edge_idx + 1) % 6, map_props.diameter_mm)
        return mw @ Vector((a[0], a[1], 0.0)), mw @ Vector((b[0], b[1], 0.0))

    @staticmethod
    def _closest_point_on_segment(p, a, b):
        ab = b - a
        t = (p - a).dot(ab) / max(ab.length_squared, 1e-9)
        t = max(0.0, min(1.0, t))
        return a + ab * t

    @staticmethod
    def _point_in_tile_local(tile, world_point, diameter_mm):
        lp = tile.matrix_world.inverted() @ world_point
        return point_in_hex(lp.x, lp.y, diameter_mm)

    def _commit_piece(self, context, seg, translation, rotation):
        mesh = _get_or_import_segment_mesh(seg["file"])
        if mesh is None:
            self.report({'ERROR'}, f"Could not import {seg['file']!r}")
            return

        map_props = context.scene.hexfinity_map
        coll = map_props.root_collection
        obj = bpy.data.objects.new("HF_SegmentPiece", mesh)
        coll.objects.link(obj)
        obj.rotation_euler = rotation.to_euler('XYZ')
        obj.location = translation
        context.view_layer.update()

        connectors_world = [
            translation + rotation @ Vector((x, y, 0.0))
            for (x, y) in _connector_locals(seg)
        ]
        # Whichever specific open connector this placement actually snapped
        # onto (_connects_to_prev already required it to be one of
        # self._open_connectors when that list was non-empty) is the one
        # being consumed by this join -- not just "the" single tracked
        # connector, since a piece can leave more than one open.
        consumed = None
        if self._snap_meta is not None and self._snap_meta[0] == 'open':
            consumed = self._snap_meta[1]["world"]
        remaining = [w for w in connectors_world
                    if consumed is None or (w - consumed).length > 1e-3]

        pieces = self._clip_to_hexes(context, obj, map_props)
        if not pieces:
            bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'WARNING'}, "Segment falls outside the generated map")
            return

        crossing_neighbour = None
        if len(pieces) == 2:
            (tile_a, _obj_a), (tile_b, _obj_b) = pieces
            edge_idx = self._shared_edge_idx(tile_a, tile_b)
            if edge_idx is not None:
                a1, a2 = self._edge_endpoints_world(context, tile_a, edge_idx)
                crossing_xy = self._closest_point_on_segment(translation, a1, a2)
                crossing_z = self._surface_z_at(context, tile_a, crossing_xy.x, crossing_xy.y)
                crossing_point = Vector((crossing_xy.x, crossing_xy.y, crossing_z))
                remaining.append(crossing_point)
                crossing_neighbour = tile_b

        from . import properties

        run_open_connectors = []
        for tile, piece_obj in pieces:
            piece_obj[SEGMENT_PIECE_TAG] = True
            piece_obj[SEGMENT_TYPE_NAME] = self.type_name
            piece_obj[SEGMENT_SOURCE_FILE] = seg["file"]
            world = piece_obj.matrix_world.copy()
            piece_obj.parent = tile
            piece_obj.matrix_parent_inverse = tile.matrix_world.inverted()
            piece_obj.matrix_world = world

            feature = tile.hexfinity_tile.path_features.add()
            feature.name = f"Segment {len(tile.hexfinity_tile.path_features)}"
            properties._PATH_FEATURE_FILLING = True
            try:
                feature.feature_type = 'SEGMENT'
            finally:
                properties._PATH_FEATURE_FILLING = False
            feature.segment_run_id = self._run_id
            feature.segment_piece = piece_obj
            tile.hexfinity_tile.active_path_feature_index = (
                len(tile.hexfinity_tile.path_features) - 1)

            for w in remaining:
                if self._point_in_tile_local(tile, w, map_props.diameter_mm):
                    run_open_connectors.append({"world": w, "tile": tile})

        self._open_connectors = run_open_connectors
        bpy.ops.ed.undo_push(message="HexFinity Draw Segments Path")

        next_tile = (self._open_connectors[-1]["tile"] if self._open_connectors
                    else pieces[-1][0])

        if crossing_neighbour is not None and crossing_neighbour is not self._hover_tile:
            # Not guaranteed to already be selected -- no pre-selection is
            # required to cross onto it -- so select it too, matching
            # path_features.py's own _continue_onto convention.
            crossing_neighbour.select_set(True)
            context.view_layer.objects.active = crossing_neighbour
            self._start_view_pan(context, crossing_neighbour)
        elif next_tile is not None and next_tile is not context.view_layer.objects.active:
            next_tile.select_set(True)
            context.view_layer.objects.active = next_tile

    # -- view pan on hex crossing (mirrors path_features.py's own copy) ------

    def _start_view_pan(self, context, tile_obj):
        rv3d = context.region_data
        if rv3d is None:
            return
        self._pan_start = rv3d.view_location.copy()
        self._pan_target = tile_obj.matrix_world.translation.copy()
        self._pan_start_time = time.monotonic()
        if self._pan_timer is None:
            self._pan_timer = context.window_manager.event_timer_add(
                1.0 / 60.0, window=context.window)

    def _advance_view_pan(self, context):
        if self._pan_start is None:
            return
        rv3d = context.region_data
        if rv3d is None:
            return
        t = min(1.0, (time.monotonic() - self._pan_start_time) / VIEW_PAN_DURATION_S)
        eased = t * t * (3.0 - 2.0 * t)
        rv3d.view_location = self._pan_start.lerp(self._pan_target, eased)
        self._tag_redraw(context)
        if t >= 1.0:
            self._stop_view_pan(context)

    def _stop_view_pan(self, context):
        if self._pan_timer is not None:
            context.window_manager.event_timer_remove(self._pan_timer)
            self._pan_timer = None
        self._pan_start = None
        self._pan_target = None

    # -- teardown / ghost drawing ---------------------------------------------

    def _finish(self, context):
        global _ACTIVE
        _ACTIVE = False
        self._stop_view_pan(context)
        if self._draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.workspace.status_text_set(None)
        self._tag_redraw(context)

    def _local_triangles(self, seg):
        """Cached-per-filepath local-space triangle list for the ghost
        preview. Built from `seg`'s own authored hull (`hull_local_mm`,
        captured once at authoring time -- see docs/settings.md) via
        `segment_geometry.hull_prism_triangles`, spanning the real
        imported mesh's own Z range (read once, not re-derived every
        frame) -- a couple dozen triangles regardless of how high-poly
        the actual STL is, since the live drag preview only needs to
        convey position/orientation/footprint, not exact shape (the
        committed piece always uses the real geometry; this only ever
        backs `_draw`). Falls back to the real mesh's own triangles for a
        pre-hull settings.json entry or a degenerate hull."""
        filepath = seg["file"]
        cached = self._ghost_tri_cache.get(filepath)
        if cached is not None:
            return cached

        tris = None
        hull = seg.get("hull_local_mm") or []
        if len(hull) >= 3:
            mesh = _get_or_import_segment_mesh(filepath)
            if mesh is not None and len(mesh.vertices) > 0:
                z_min = min(v.co.z for v in mesh.vertices)
                z_max = max(v.co.z for v in mesh.vertices)
                hull_tris = segment_geometry.hull_prism_triangles(
                    [(x, y) for (x, y) in hull], z_min, z_max)
                if hull_tris:
                    tris = [tuple(Vector(v) for v in tri) for tri in hull_tris]

        if tris is None:
            mesh = _get_or_import_segment_mesh(filepath)
            tris = []
            if mesh is not None:
                mesh.calc_loop_triangles()
                for lt in mesh.loop_triangles:
                    tris.append(tuple(mesh.vertices[i].co.copy() for i in lt.vertices))

        self._ghost_tri_cache[filepath] = tris
        return tris

    def _draw(self, context):
        if self._transform is None:
            return
        translation, rotation = self._transform
        seg = self._current_segment()
        tris = self._local_triangles(seg)
        if not tris:
            return

        def to_world(v):
            w = translation + rotation @ v
            return (w.x, w.y, w.z)

        positions = []
        for tri in tris:
            for v in tri:
                positions.append(to_world(v))

        color = _GHOST_SNAP_COLOR if self._snap_world is not None else _GHOST_COLOR
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.face_culling_set('NONE')
        shader.bind()
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'TRIS', {"pos": positions}).draw(shader)

        # Snap-target markers, drawn in the same POST_VIEW world space as
        # the ghost above -- no screen-space projection needed, unlike
        # path_features.py's POST_PIXEL tip dot. Every available candidate
        # (hex-edge points + this run's open connectors) is shown dim and
        # small so the user can see where there's something to aim for;
        # the one currently engaged (if any) is drawn brighter and bigger,
        # on top, so the exact snap point is unambiguous.
        if self._hover_targets:
            gpu.state.point_size_set(6.0)
            shader.uniform_float("color", _SNAP_CANDIDATE_COLOR)
            candidate_positions = [(w.x, w.y, w.z) for w, _meta in self._hover_targets]
            batch_for_shader(shader, 'POINTS', {"pos": candidate_positions}).draw(shader)
        if self._snap_world is not None:
            gpu.state.point_size_set(12.0)
            shader.uniform_float("color", _SNAP_ACTIVE_COLOR)
            w = self._snap_world
            batch_for_shader(shader, 'POINTS', {"pos": [(w.x, w.y, w.z)]}).draw(shader)

        gpu.state.depth_test_set('NONE')
        gpu.state.face_culling_set('NONE')
        gpu.state.blend_set('NONE')
