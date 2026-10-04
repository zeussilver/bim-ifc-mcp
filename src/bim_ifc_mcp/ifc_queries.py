"""Deterministic IFC queries and rule checks.

Everything here is plain code over IfcOpenShell: same model in, same answer out.
The MCP layer (server.py) only exposes these functions; the LLM never computes
areas or pass/fail itself, it only explains what these functions return.
"""

from functools import lru_cache
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.element as element_util
import ifcopenshell.util.unit as unit_util

from bim_ifc_mcp.geometry import space_footprint_areas

# Revit's IFC exporter and IFC4 templates use different quantity set names.
SPACE_AREA_SOURCES = [
    ("Qto_SpaceBaseQuantities", "NetFloorArea"),
    ("BaseQuantities", "NetFloorArea"),
    ("Qto_SpaceBaseQuantities", "GrossFloorArea"),
    ("BaseQuantities", "GrossFloorArea"),
]
# Revit fills IfcDoor.Tag with its ElementId; the mark on the drawings is the "Mark"
# parameter, exported in one of these property sets (newer / older exporters).
REVIT_MARK_PSETS = ["Identity Data", "PSet_Revit_Identity Data"]


@lru_cache(maxsize=4)
def _open_cached(path: str, mtime: float) -> ifcopenshell.file:
    return ifcopenshell.open(path)


def open_model(path: str | Path) -> ifcopenshell.file:
    """Open an IFC file, reusing the parsed model until the file changes on disk."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"IFC file not found: {p}")
    return _open_cached(str(p), p.stat().st_mtime)


def _to_mm(model: ifcopenshell.file, value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * unit_util.calculate_unit_scale(model, "LENGTHUNIT") * 1000, 1)


def _to_m2(model: ifcopenshell.file, value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * unit_util.calculate_unit_scale(model, "AREAUNIT"), 2)


def _storey_name(el) -> str | None:
    """Walk up containment/aggregation until we hit a storey."""
    parent = element_util.get_container(el) or element_util.get_aggregate(el)
    while parent is not None and not parent.is_a("IfcBuildingStorey"):
        parent = element_util.get_aggregate(parent)
    return parent.Name if parent is not None else None


def _host_element(el):
    """The element whose opening `el` fills, e.g. the wall a door sits in (same in IFC2X3 and IFC4)."""
    for rel in el.FillsVoids or ():
        for void in rel.RelatingOpeningElement.VoidsElements or ():
            return void.RelatingBuildingElement
    return None


def _mark(el, psets: dict) -> str | None:
    """The element's Revit Mark when exported, else its Tag."""
    for pset in REVIT_MARK_PSETS:
        mark = psets.get(pset, {}).get("Mark")
        if mark not in (None, ""):
            return str(mark)
    return el.Tag


def model_summary(model: ifcopenshell.file) -> dict:
    project = model.by_type("IfcProject")[0]
    counts = {
        cls: len(model.by_type(cls))
        for cls in ("IfcBuildingStorey", "IfcSpace", "IfcDoor", "IfcWindow", "IfcWall", "IfcSlab")
    }
    return {
        "schema": model.schema,
        "project": project.Name,
        "storeys": [s.Name for s in model.by_type("IfcBuildingStorey")],
        "element_counts": counts,
    }


def list_spaces(model: ifcopenshell.file) -> list[dict]:
    rows = []
    for space in model.by_type("IfcSpace"):
        qtos = element_util.get_psets(space, qtos_only=True)
        area, source = None, None
        for qto_name, key in SPACE_AREA_SOURCES:
            value = qtos.get(qto_name, {}).get(key)
            if value is not None:
                area, source = _to_m2(model, value), f"{qto_name}.{key}"
                break
        rows.append({
            "global_id": space.GlobalId,
            "number": space.Name,
            "name": space.LongName,
            "storey": _storey_name(space),
            "area_m2": area,
            "area_source": source,
        })

    # No quantity (typical of Revit exports): fall back to the space geometry's footprint.
    missing = [r for r in rows if r["area_m2"] is None]
    if missing:
        areas = space_footprint_areas(model, (r["global_id"] for r in missing))
        for r in missing:
            if r["global_id"] in areas:
                r["area_m2"], r["area_source"] = areas[r["global_id"]], "geometry:footprint"
    return sorted(rows, key=lambda r: (r["storey"] or "", r["number"] or ""))


def door_schedule(model: ifcopenshell.file) -> list[dict]:
    rows = []
    for door in model.by_type("IfcDoor"):
        psets = element_util.get_psets(door, psets_only=True)
        common = psets.get("Pset_DoorCommon", {})
        door_type = element_util.get_type(door)  # IfcDoorType (IFC4) or IfcDoorStyle (IFC2X3)
        host = _host_element(door)
        host_rating = None
        if host is not None and host.is_a("IfcWall"):
            host_rating = element_util.get_psets(host, psets_only=True).get("Pset_WallCommon", {}).get("FireRating")
        rows.append({
            "global_id": door.GlobalId,
            "mark": _mark(door, psets),
            "tag": door.Tag,  # Revit ElementId in Revit exports, for "Select by ID"
            "name": door.Name,
            "type_name": door_type.Name if door_type is not None else None,
            "storey": _storey_name(door),
            "width_mm": _to_mm(model, door.OverallWidth),
            "height_mm": _to_mm(model, door.OverallHeight),
            "fire_rating": common.get("FireRating"),
            "self_closing": common.get("SelfClosing"),
            "fire_exit": common.get("FireExit"),
            "is_external": common.get("IsExternal"),
            "host_wall_fire_rating": host_rating,
        })
    return sorted(rows, key=lambda r: (r["storey"] or "", r["mark"] or ""))


def door_width_status(width_mm: float | None, min_width_mm: float) -> tuple[str, str]:
    """PASS / FAIL / UNKNOWN plus a human-readable reason for one door width.

    OverallWidth is the door's overall (nominal) width, never less than the clear opening: a FAIL
    is definite, a PASS only means the door is not ruled out.
    """
    if width_mm is None:
        return "UNKNOWN", "OverallWidth missing in model"
    if width_mm >= min_width_mm:
        return "PASS", f"{width_mm} mm >= {min_width_mm} mm (overall width; confirm clear opening)"
    return "FAIL", f"{width_mm} mm < {min_width_mm} mm (short by {round(min_width_mm - width_mm, 1)} mm)"


def check_door_widths(model: ifcopenshell.file, min_width_mm: float = 850) -> dict:
    """Rule: every door's width must be >= min_width_mm.

    Uses IfcDoor.OverallWidth, the overall (nominal) width, so a PASS here is a
    screening result, not a certified clear-opening measurement.
    """
    results = []
    for door in door_schedule(model):
        status, reason = door_width_status(door["width_mm"], min_width_mm)
        results.append({**door, "status": status, "reason": reason})

    summary = {s: sum(r["status"] == s for r in results) for s in ("PASS", "FAIL", "UNKNOWN")}
    return {
        "rule": f"Door width >= {min_width_mm} mm",
        "measured_attribute": "IfcDoor.OverallWidth (overall width; not clear opening)",
        "summary": summary,
        "results": results,
    }
