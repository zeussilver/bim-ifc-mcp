"""Floor areas, bounding boxes and sight lines computed from IFC geometry.

Real Revit exports often ship spaces without base quantities ("Export base
quantities" is off by default), so there is no NetFloorArea to read. A space's
Body is an extrusion of its room boundary, so its top-down footprint is the
floor area. ifcopenshell.geom returns SI metres whatever the project units, so
the result is m2 for IFC2X3 and IFC4 alike.

Revit also places most IfcSpaces at their storey origin and draws the room offset
from there, so only the geometry says where a room is: BCF cameras frame an
element's bounding box, and sight lines are tested against the triangulated
model so a camera is not put behind a wall or a staircase.
"""

from collections.abc import Iterable
from functools import lru_cache

import numpy as np
import shapely

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.shape as shape_util


def space_footprint_areas(model: ifcopenshell.file, global_ids: Iterable[str]) -> dict[str, float]:
    """{GlobalId: footprint area in m2, 2 dp}. Spaces without usable geometry are left out."""
    return dict(_footprint_areas(model, frozenset(global_ids)))


def footprint_area(geometry) -> float:
    """Area of the XY projection of a triangulated shape (world coordinates, m2).

    Projects every non-vertical triangle whichever way it faces.
    ifcopenshell.util.shape.get_footprint_area only counts upward-facing
    triangles, which gives 0 for Revit surface-model spaces with flipped faces.
    """
    xy = shape_util.get_vertices(geometry, is_2d=True)
    triangles = xy[shape_util.get_faces(geometry)]  # (n, 3, 2)
    e1, e2 = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    flat = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) > 1e-9  # drop vertical (zero-area) triangles
    if not flat.any():
        return 0.0
    return shapely.union_all(shapely.polygons(triangles[flat])).area


def element_bboxes(model: ifcopenshell.file, elements: Iterable) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """{GlobalId: (min corner, max corner) of the element's world bounding box in metres}.

    Elements without geometry are left out.
    """
    boxes = {}
    for shape in _world_shapes(model, elements):
        verts = shape_util.get_vertices(shape.geometry)
        if len(verts):
            boxes[shape.guid] = (verts.min(axis=0), verts.max(axis=0))
    return boxes


@lru_cache(maxsize=4)
def scene_mesh(model: ifcopenshell.file) -> tuple[np.ndarray, np.ndarray]:
    """World triangles (n, 3, 3) in metres of every physical element, and the GlobalId owning each.

    Openings and virtual elements are left out; so are spaces, which are not IfcElements.
    """
    elements = [e for e in model.by_type("IfcElement")
                if not e.is_a("IfcFeatureElement") and not e.is_a("IfcVirtualElement")]
    triangles, owners = [], []
    for shape in _world_shapes(model, elements):
        tris = shape_util.get_vertices(shape.geometry)[shape_util.get_faces(shape.geometry)]
        triangles.append(tris)
        owners.append(np.full(len(tris), shape.guid, dtype=object))
    if not triangles:
        return np.empty((0, 3, 3)), np.empty(0, dtype=object)
    return np.concatenate(triangles), np.concatenate(owners)


def blocked_sightlines(mesh: tuple[np.ndarray, np.ndarray], eye, targets, *, below_z: float,
                       ignore: str) -> int:
    """How many eye -> target segments hit a triangle, ignoring the element `ignore` and any hit
    above `below_z` (geometry a clipping plane hides). Moller-Trumbore, vectorised per segment."""
    triangles, owners = mesh
    eye, targets = np.asarray(eye, float), np.asarray(targets, float)
    lo = np.minimum(eye, targets.min(axis=0))
    hi = np.maximum(eye, targets.max(axis=0))
    near = ((triangles.max(axis=1) >= lo) & (triangles.min(axis=1) <= hi)).all(axis=1)
    tris = triangles[near & (owners != ignore) & (triangles[:, :, 2].min(axis=1) <= below_z)]
    if not len(tris):
        return 0
    v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    s = eye - v0
    q = np.cross(s, e1)
    blocked = 0
    for target in targets:
        ray = target - eye
        p = np.cross(ray, e2)
        det = (e1 * p).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):  # det == 0: ray parallel to the triangle
            u = (s * p).sum(axis=1) / det
            v = (q @ ray) / det
            t = (e2 * q).sum(axis=1) / det
            hit = (np.abs(det) > 1e-12) & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-6) & (t < 1 - 1e-6)
            blocked += bool((hit & (eye[2] + t * ray[2] <= below_z)).any())
    return blocked


@lru_cache(maxsize=4)
def _footprint_areas(model: ifcopenshell.file, global_ids: frozenset[str]) -> dict[str, float]:
    areas = {}
    for shape in _world_shapes(model, map(model.by_guid, global_ids)):
        area = footprint_area(shape.geometry)
        if area > 0:
            areas[shape.guid] = round(area, 2)
    return areas


def _world_shapes(model: ifcopenshell.file, elements: Iterable):
    """Triangulated shapes of the elements in world coordinates, in one ifcopenshell.geom pass."""
    elements = [e for e in elements if getattr(e, "Representation", None)]
    if not elements:
        return
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)  # project along the true vertical, whatever the placements
    iterator = ifcopenshell.geom.iterator(settings, model, include=elements)
    if not iterator.initialize():  # nothing could be triangulated
        return
    while True:
        yield iterator.get()
        if not iterator.next():
            return
