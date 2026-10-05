"""Pinned behaviour on two real third-party exports (fetched by scripts/fetch_samples.py, sha256-pinned).

  - Duplex_A_20110907.ifc  IFC2X3, Autodesk Revit Architecture 2011: no area quantities, Revit door
    marks in a property set, the literal text "Fire Rating" as every door's FireRating.
  - AC20-FZK-Haus.ifc      IFC4, ArchiCAD 20: base quantities present, German room names.

The numbers below were read off the tool output and cross-checked against what each authoring
tool exported itself, so a change in any of them is a behaviour change, not noise.
"""

import csv
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import ifcopenshell.util.element as element_util
import ifcopenshell.util.placement as placement_util
import ifcopenshell.util.unit as unit_util
import numpy as np
import pytest
from mcp import Client

from bim_ifc_mcp import geometry
from bim_ifc_mcp import ifc_queries as q
from bim_ifc_mcp import rules as r
from bim_ifc_mcp.bcf import issues_to_bcf
from bim_ifc_mcp.export import issues_to_csv
from bim_ifc_mcp.server import mcp

ROOT = Path(__file__).resolve().parent.parent
DUPLEX = ROOT / "models" / "external" / "Duplex_A_20110907.ifc"
FZK = ROOT / "models" / "external" / "AC20-FZK-Haus.ifc"
FZK_RULES = ROOT / "rules" / "fzk_haus_de.toml"

pytestmark = pytest.mark.skipif(not (DUPLEX.is_file() and FZK.is_file()), reason="run scripts/fetch_samples.py")

# Stricter than the defaults so the FZK-Haus export has door and room topics to check (defaults raise none).
FZK_STRICT = {
    "name": "FZK strict (test)",
    "rules": [
        {"id": "DOOR-900", "type": "door_min_width", "title": "Door width >= 900 mm", "min_width_mm": 900},
        {"id": "ROOM-17", "type": "space_min_area", "title": "Room >= 17 m2", "min_area_m2": 17},
    ],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def duplex():
    return q.open_model(DUPLEX)


@pytest.fixture(scope="module")
def fzk():
    return q.open_model(FZK)


@pytest.fixture(scope="module")
def duplex_spaces(duplex):
    return {row["number"]: row for row in q.list_spaces(duplex)}


def _body(space):
    return next(rep for rep in space.Representation.Representations if rep.RepresentationIdentifier == "Body")


def _extrusion_direction(space, solid) -> np.ndarray:
    """World direction of an IfcExtrudedAreaSolid (object placement, then solid position)."""
    rotation = (placement_util.get_local_placement(space.ObjectPlacement)[:3, :3]
                @ placement_util.get_axis2placement(solid.Position)[:3, :3])
    return rotation @ np.array(solid.ExtrudedDirection.DirectionRatios, dtype=float)


# --- Duplex (Revit 2011, IFC2X3) ---------------------------------------------------------------

def test_duplex_schema_and_counts(duplex):
    s = q.model_summary(duplex)
    assert s["schema"] == "IFC2X3"
    assert s["storeys"] == ["Level 1", "Level 2", "T/FDN", "Roof"]
    assert s["element_counts"] == {"IfcBuildingStorey": 4, "IfcSpace": 21, "IfcDoor": 14, "IfcWindow": 24,
                                   "IfcWall": 57, "IfcSlab": 21}


def test_duplex_every_space_area_comes_from_geometry(duplex_spaces):
    # Revit exported no Qto/BaseQuantities, so every area is a computed footprint.
    assert len(duplex_spaces) == 21
    assert {row["area_source"] for row in duplex_spaces.values()} == {"geometry:footprint"}
    assert all(row["area_m2"] > 0 for row in duplex_spaces.values())


def test_duplex_surface_model_hallways(duplex, duplex_spaces):
    # Faces of these two surface models point down; upward-face-only footprints returned 0 for them.
    hallways = [s for s in duplex.by_type("IfcSpace") if _body(s).RepresentationType == "SurfaceModel"]
    assert sorted(s.Name for s in hallways) == ["A201", "B201"]
    for s in hallways:
        assert duplex_spaces[s.Name]["name"] == "Hallway"
        assert duplex_spaces[s.Name]["area_m2"] == pytest.approx(6.89, abs=0.01)


def test_duplex_footprint_matches_revit_volume_over_depth(duplex, duplex_spaces):
    """For a vertical extrusion, Revit's own Volume / extrusion depth is the floor area it modelled.

    Revit's Area parameter is not comparable: its room-area boundary differs, and the footprint is
    6.8-22.8 % below it here. The two stairs are drawn as a vertical section extruded sideways, so
    Volume / depth is the section area (20.5 m2), not the floor area; they are excluded, and pinned
    so the filter cannot silently drop more rooms.
    """
    assert unit_util.calculate_unit_scale(duplex) == 1.0  # metres: m3 / m = m2
    compared, sideways = {}, set()
    for space in duplex.by_type("IfcSpace"):
        body = _body(space)
        if body.RepresentationType != "SweptSolid":
            continue
        (solid,) = body.Items
        if abs(_extrusion_direction(space, solid)[2]) < 0.999:
            sideways.add(space.Name)
            continue
        volume = element_util.get_psets(space)["PSet_Revit_Dimensions"]["Volume"]
        compared[space.Name] = volume / solid.Depth
    assert sideways == {"A105", "B105"}
    assert len(compared) == 17
    for name, revit_area in compared.items():
        assert duplex_spaces[name]["area_m2"] == pytest.approx(revit_area, rel=0.01), name


def test_duplex_door_marks_are_revit_marks_not_element_ids(duplex):
    doors = q.door_schedule(duplex)
    assert len(doors) == 14
    for d in doors:
        assert re.fullmatch(r"[AB]\d{3}", d["mark"]), d
        assert d["tag"].isdigit() and d["tag"] != d["mark"]  # IfcDoor.Tag holds the Revit ElementId


def test_duplex_default_rules(duplex):
    report = r.run_rules(duplex, r.load_rules())
    assert report["summary"] == {"PASS": 12, "FAIL": 6, "UNKNOWN": 14}
    assert [(rule["id"], rule["checked"], rule["summary"]) for rule in report["rules"]] == [
        ("DOOR-WIDTH-01", 14, {"PASS": 8, "FAIL": 6, "UNKNOWN": 0}),
        ("SPACE-AREA-01", 4, {"PASS": 4, "FAIL": 0, "UNKNOWN": 0}),  # Bedroom 1/2 in both units
        ("FIRE-DOOR-01", 14, {"PASS": 0, "FAIL": 0, "UNKNOWN": 14}),
        ("FIRE-DOOR-02", 0, {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}),  # no wall carries a FireRating
    ]
    assert len(report["issues"]) == 20
    width_fails = [(i["element"], i["message"]) for i in report["issues"] if i["rule_id"] == "DOOR-WIDTH-01"]
    assert width_fails == [
        ("A102", "813.0 mm < 850 mm (short by 37.0 mm)"),
        ("A104", "762.0 mm < 850 mm (short by 88.0 mm)"),
        ("B102", "813.0 mm < 850 mm (short by 37.0 mm)"),
        ("B104", "762.0 mm < 850 mm (short by 88.0 mm)"),
        ("A205", "762.0 mm < 850 mm (short by 88.0 mm)"),
        ("B205", "762.0 mm < 850 mm (short by 88.0 mm)"),
    ]


def test_duplex_fire_door_issues_are_placeholder_unknowns(duplex):
    # Revit 2011 exported the parameter's name, "Fire Rating", as the value on every door.
    issues = [i for i in r.run_rules(duplex, r.load_rules())["issues"] if i["rule_id"] == "FIRE-DOOR-01"]
    assert len(issues) == 14
    for i in issues:
        assert i["status"] == "UNKNOWN"
        assert i["message"] == 'FireRating holds placeholder "Fire Rating"; confirm whether this door is fire-rated'


@pytest.mark.anyio
async def test_duplex_run_rules_over_mcp(duplex):
    async with Client(mcp) as client:
        result = await client.call_tool("run_rules", {"ifc_path": str(DUPLEX)})
    assert not result.is_error, result.content[0].text
    assert json.loads(result.content[0].text) == r.run_rules(duplex, r.load_rules())


# --- FZK-Haus (ArchiCAD 20, IFC4) --------------------------------------------------------------

def test_fzk_schema_and_quantity_areas(fzk):
    assert q.model_summary(fzk)["schema"] == "IFC4"
    spaces = {row["name"]: row for row in q.list_spaces(fzk)}
    assert len(spaces) == 7
    assert {row["area_source"] for row in spaces.values()} == {"BaseQuantities.NetFloorArea"}
    assert spaces["Schlafzimmer"]["area_m2"] == 21.41


def test_fzk_footprint_is_gross_not_net_floor_area(fzk):
    # The computed footprint matches ArchiCAD's GrossFloorArea; NetFloorArea follows its own
    # definition (finish deduction, headroom under the roof), so the two must not be confused.
    spaces = fzk.by_type("IfcSpace")
    footprints = geometry.space_footprint_areas(fzk, [s.GlobalId for s in spaces])
    for s in spaces:
        qto = element_util.get_psets(s, qtos_only=True)["BaseQuantities"]
        assert footprints[s.GlobalId] == pytest.approx(qto["GrossFloorArea"], abs=0.01), s.LongName
        assert qto["NetFloorArea"] <= qto["GrossFloorArea"]
    galerie = next(s for s in spaces if s.LongName == "Galerie")
    assert footprints[galerie.GlobalId] == 107.16
    assert element_util.get_psets(galerie, qtos_only=True)["BaseQuantities"]["NetFloorArea"] == pytest.approx(74.51, abs=0.01)


def test_fzk_default_bedroom_rule_checks_nothing(fzk):
    report = r.run_rules(fzk, r.load_rules())
    rules = {rule["id"]: rule for rule in report["rules"]}
    assert rules["SPACE-AREA-01"]["checked"] == 0  # "bed" matches no German room name
    assert report["summary"] == {"PASS": 5, "FAIL": 0, "UNKNOWN": 0}
    assert report["issues"] == []


def test_fzk_project_rules_check_the_bedroom(fzk):
    report = r.run_rules(fzk, r.load_rules(FZK_RULES))
    rules = {rule["id"]: rule for rule in report["rules"]}
    assert rules["SPACE-AREA-01"]["checked"] == 1
    assert rules["SPACE-AREA-01"]["summary"] == {"PASS": 1, "FAIL": 0, "UNKNOWN": 0}
    assert report["summary"] == {"PASS": 6, "FAIL": 0, "UNKNOWN": 0}


# --- Exports of both real models ---------------------------------------------------------------

@pytest.mark.parametrize(("path", "config", "issue_count"), [
    pytest.param(DUPLEX, None, 20, id="Duplex-default"),
    pytest.param(FZK, FZK_STRICT, 7, id="FZK-strict"),
])
def test_csv_and_bcf_export(tmp_path, path, config, issue_count):
    model = q.open_model(path)
    issues = r.run_rules(model, config or r.load_rules())["issues"]
    assert len(issues) == issue_count

    csv_path = issues_to_csv(issues, tmp_path / "issues.csv")
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [(row["rule_id"], row["element"], row["status"]) for row in rows] == [
        (i["rule_id"], i["element"], i["status"]) for i in issues
    ]

    bcf_path = issues_to_bcf(issues, tmp_path / "issues.bcf", model=model, ifc_filename=path.name)
    mesh = geometry.scene_mesh(model)
    boxes = geometry.element_bboxes(model, [model.by_guid(i["global_id"]) for i in issues])
    with zipfile.ZipFile(bcf_path) as zf:
        topics = sorted({name.split("/")[0] for name in zf.namelist() if "/" in name})
        assert len(topics) == issue_count
        selected = set()
        for topic in topics:
            visinfo = ET.fromstring(zf.read(f"{topic}/viewpoint.bcfv"))
            guid = visinfo.find("Components/Selection/Component").get("IfcGuid")
            selected.add(guid)
            assert f"{topic}/snapshot.png" in zf.namelist(), topic  # BIMcollab needs it to open the viewpoint
            eye = [float(visinfo.findtext(f"PerspectiveCamera/CameraViewPoint/{a}")) for a in "XYZ"]
            clip_z = float(visinfo.findtext("ClippingPlanes/ClippingPlane/Location/Z"))
            # BIMcollab Zoom showed the first camera (a fixed offset) buried in the floor above door A104,
            # and the next one with the foot of A104 behind the wall in front. Now nothing below the
            # clipping plane stands between the camera and the element's centre or its foot.
            lo, hi = boxes[guid]
            centre = (lo + hi) / 2
            foot = [centre[0], centre[1], lo[2] + 0.02]
            assert geometry.blocked_sightlines(mesh, eye, [centre, foot], below_z=clip_z, ignore=guid) == 0, topic
    assert selected == {i["global_id"] for i in issues}


def test_fzk_csv_keeps_german_room_names(tmp_path):
    issues = r.run_rules(q.open_model(FZK), FZK_STRICT)["issues"]
    with issues_to_csv(issues, tmp_path / "issues.csv").open(encoding="utf-8-sig", newline="") as f:
        names = {row["element_name"] for row in csv.DictReader(f) if row["ifc_class"] == "IfcSpace"}
    assert names == {"Flur", "Buero", "Bad", "Küche"}
