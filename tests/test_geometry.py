"""Floor-area fallback from space geometry, for exports without area quantities."""

from pathlib import Path
from types import SimpleNamespace

import ifcopenshell
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.util.unit
import numpy as np
import pytest

from bim_ifc_mcp import geometry
from bim_ifc_mcp import ifc_queries as q

MODELS = Path(__file__).resolve().parent.parent / "models"
# dx * dy of each space box in scripts/make_sample.py
FOOTPRINTS = {"G01": 32.5, "G02": 14.2, "G03": 4.8, "101": 15.0, "102": 8.4}


@pytest.mark.parametrize("name", ["sample_house.ifc", "sample_house_ifc2x3.ifc"])
def test_footprint_area_of_every_space(name):
    model = q.open_model(MODELS / name)
    assert ifcopenshell.util.unit.calculate_unit_scale(model) == 0.001  # mm project, yet the result is m2
    spaces = model.by_type("IfcSpace")
    areas = geometry.space_footprint_areas(model, [s.GlobalId for s in spaces])
    assert {s.Name: areas[s.GlobalId] for s in spaces} == pytest.approx(FOOTPRINTS, abs=0.01)


def test_footprint_ignores_face_orientation():
    # 3.0 x 2.8 m surface model whose floor AND ceiling face down, as in some Revit IFC2X3
    # spaces; ifcopenshell.util.shape.get_footprint_area returns 0 for this.
    verts = [(0, 0, 0), (3, 0, 0), (3, 2.8, 0), (0, 2.8, 0), (0, 0, 2.7), (3, 0, 2.7), (3, 2.8, 2.7), (0, 2.8, 2.7)]
    floor, ceiling, wall = [(0, 2, 1), (0, 3, 2)], [(4, 6, 5), (4, 7, 6)], [(0, 1, 5)]
    mesh = SimpleNamespace(
        verts_buffer=np.array(verts, "d").tobytes(), faces_buffer=np.array(floor + ceiling + wall, "i").tobytes()
    )
    assert geometry.footprint_area(mesh) == pytest.approx(8.4)


def test_geometry_only_for_spaces_without_quantity(monkeypatch):
    model = q.open_model(MODELS / "sample_house.ifc")
    calls = []

    def spy(m, ids):
        calls.append(ids := list(ids))
        return geometry.space_footprint_areas(m, ids)

    monkeypatch.setattr(q, "space_footprint_areas", spy)
    q.list_spaces(model)
    assert calls == [[s.GlobalId for s in model.by_type("IfcSpace") if s.Name == "102"]]


def test_no_geometry_when_every_space_has_a_quantity(monkeypatch):
    model = ifcopenshell.open(str(MODELS / "sample_house.ifc"))  # private copy: we edit it
    space = next(s for s in model.by_type("IfcSpace") if s.Name == "102")
    qto = ifcopenshell.api.pset.add_qto(model, product=space, name="Qto_SpaceBaseQuantities")
    ifcopenshell.api.pset.edit_qto(model, qto=qto, properties={"NetFloorArea": 8.0})
    monkeypatch.setattr(q, "space_footprint_areas", lambda *a: pytest.fail("geometry should not run"))
    assert {r["number"]: r["area_m2"] for r in q.list_spaces(model)}["102"] == 8.0


def test_space_without_representation_has_no_area():
    model = ifcopenshell.open(str(MODELS / "sample_house_ifc2x3.ifc"))  # private copy: we edit it
    ifcopenshell.api.root.create_entity(model, ifc_class="IfcSpace", name="X01")
    spaces = {r["number"]: r for r in q.list_spaces(model)}
    assert (spaces["X01"]["area_m2"], spaces["X01"]["area_source"]) == (None, None)
    assert spaces["102"]["area_m2"] == 8.4
