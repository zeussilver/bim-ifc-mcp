from pathlib import Path

import ifcopenshell
import ifcopenshell.api.pset
import pytest

from bim_ifc_mcp import ifc_queries as q

MODELS = Path(__file__).resolve().parent.parent / "models"
SAMPLE = MODELS / "sample_house.ifc"
SAMPLE_2X3 = MODELS / "sample_house_ifc2x3.ifc"
DUPLEX = MODELS / "external" / "Duplex_A_20110907.ifc"


@pytest.fixture(scope="module")
def model():
    return q.open_model(SAMPLE)


@pytest.fixture(scope="module", params=[SAMPLE, SAMPLE_2X3], ids=["IFC4", "IFC2X3"])
def any_model(request):
    """The same building in both schemas: door answers must not depend on the schema."""
    return q.open_model(request.param)


def test_summary(model):
    s = q.model_summary(model)
    assert s["schema"] == "IFC4"
    assert s["storeys"] == ["Ground Floor", "Level 1"]
    assert s["element_counts"]["IfcDoor"] == 5
    assert s["element_counts"]["IfcSpace"] == 5


def test_summary_ifc2x3():
    s = q.model_summary(q.open_model(SAMPLE_2X3))
    assert s["schema"] == "IFC2X3"
    assert s["project"] == "Sample House (IFC2X3)"
    assert s["storeys"] == ["Ground Floor", "Level 1"]
    assert s["element_counts"]["IfcDoor"] == 5
    assert s["element_counts"]["IfcSpace"] == 5


def test_spaces_have_areas_and_storeys(model):
    spaces = {r["number"]: r for r in q.list_spaces(model)}
    assert spaces["G01"]["area_m2"] == 32.5
    assert spaces["G01"]["area_source"] == "Qto_SpaceBaseQuantities.NetFloorArea"
    assert spaces["G03"]["name"] == "Accessible WC"
    assert spaces["101"]["storey"] == "Level 1"


def test_space_without_quantity_uses_geometry(model):
    space = {r["number"]: r for r in q.list_spaces(model)}["102"]
    assert space["name"] == "Bedroom 2"
    assert space["storey"] == "Level 1"
    assert space["area_m2"] == 8.4
    assert space["area_source"] == "geometry:footprint"


def test_ifc2x3_areas_come_from_geometry_and_match_ifc4(model):
    ifc4 = {r["number"]: r["area_m2"] for r in q.list_spaces(model)}
    rows = q.list_spaces(q.open_model(SAMPLE_2X3))
    assert {r["number"] for r in rows} == set(ifc4)
    for r in rows:
        assert r["area_source"] == "geometry:footprint"
        assert r["area_m2"] == pytest.approx(ifc4[r["number"]], abs=0.01)


def test_door_schedule_in_mm(any_model):
    doors = {r["mark"]: r for r in q.door_schedule(any_model)}
    assert doors["D02"]["width_mm"] == 870
    assert doors["D05"]["width_mm"] is None
    assert doors["D04"]["fire_rating"] == "FRL -/30/30"
    assert doors["D01"]["is_external"] is True


def test_door_fire_fields(any_model):
    doors = {r["mark"]: r for r in q.door_schedule(any_model)}
    assert doors["D04"]["self_closing"] is False
    assert doors["D02"]["self_closing"] is None
    assert doors["D03"]["host_wall_fire_rating"] == "FRL 60/60/60"
    assert doors["D03"]["fire_rating"] is None
    assert [m for m, d in doors.items() if d["host_wall_fire_rating"]] == ["D03"]


def test_door_type_names(model):
    # IFC4 fixture has untyped doors; the IFC2X3 one types them by IfcDoorStyle like Revit does.
    assert {r["type_name"] for r in q.door_schedule(model)} == {None}
    doors = {r["mark"]: r for r in q.door_schedule(q.open_model(SAMPLE_2X3))}
    assert doors["D01"]["type_name"] == "Entry Door"
    assert doors["D04"]["type_name"] == "WC Door"


@pytest.mark.parametrize(("path", "pset"), [(SAMPLE, "Identity Data"), (SAMPLE_2X3, "PSet_Revit_Identity Data")])
def test_door_mark_prefers_revit_mark_over_tag(path, pset):
    # Revit writes its ElementId to IfcDoor.Tag and the drawing mark to an Identity Data pset.
    model = ifcopenshell.open(str(path))  # private copy: we edit it
    door = next(d for d in model.by_type("IfcDoor") if d.Tag == "D01")
    door.Tag = "146596"
    ifcopenshell.api.pset.edit_pset(
        model, pset=ifcopenshell.api.pset.add_pset(model, product=door, name=pset), properties={"Mark": "A101"}
    )
    doors = {r["mark"]: r for r in q.door_schedule(model)}
    assert sorted(doors) == ["A101", "D02", "D03", "D04", "D05"]
    assert doors["A101"]["tag"] == "146596"  # kept for Revit "Select by ID"
    assert doors["D02"]["tag"] == "D02"  # no Revit Mark: the Tag is the mark


@pytest.mark.skipif(not DUPLEX.is_file(), reason="run scripts/fetch_samples.py")
def test_revit_export_door_marks():
    doors = {r["mark"]: r for r in q.door_schedule(q.open_model(DUPLEX))}
    assert {"A101", "B101", "A104"} <= set(doors)
    assert doors["A104"]["tag"] == "150173" and doors["A104"]["width_mm"] == 762.0


def test_door_width_rule(any_model):
    report = q.check_door_widths(any_model, min_width_mm=850)
    status = {r["mark"]: r["status"] for r in report["results"]}
    assert status == {"D01": "PASS", "D02": "PASS", "D03": "FAIL", "D04": "FAIL", "D05": "UNKNOWN"}
    assert report["summary"] == {"PASS": 2, "FAIL": 2, "UNKNOWN": 1}


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        q.open_model("does/not/exist.ifc")
