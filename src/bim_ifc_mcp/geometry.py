"""Floor areas and element centres computed from IFC geometry.

Real Revit exports often ship spaces without base quantities ("Export base
quantities" is off by default), so there is no NetFloorArea to read. A space's
Body is an extrusion of its room boundary, so its top-down footprint is the
floor area. ifcopenshell.geom returns SI metres whatever the project units, so
the result is m2 for IFC2X3 and IFC4 alike.

Revit also places most IfcSpaces at their storey origin and draws the room offset
from there, so only the geometry says where a room is; BCF cameras aim at the
centre of its bounding box.
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


def bbox_centres(model: ifcopenshell.file, elements: Iterable) -> dict[str, list[float]]:
    """{GlobalId: centre of the element's world bounding box in metres}. Elements without geometry are left out."""
    centres = {}
    for shape in _world_shapes(model, elements):
        verts = shape_util.get_vertices(shape.geometry)
        if len(verts):
            centres[shape.guid] = ((verts.min(axis=0) + verts.max(axis=0)) / 2).tolist()
    return centres


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
